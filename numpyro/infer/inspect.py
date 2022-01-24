# Copyright Contributors to the Pyro project.
# SPDX-License-Identifier: Apache-2.0

from functools import partial
from typing import Callable, Dict, Optional

import jax

from numpyro import handlers
import numpyro.distributions as dist
from numpyro.infer.initialization import init_to_sample
from numpyro.ops.provenance import ProvenanceArray, eval_provenance, get_provenance
from numpyro.ops.pytree import PytreeTrace


def is_sample_site(msg):
    if msg["type"] != "sample":
        return False

    # Exclude deterministic sites.
    if msg["fn_name"] == "Delta":
        return False

    return True


def _get_dist_name(fn):
    if isinstance(
        fn, (dist.Independent, dist.ExpandedDistribution, dist.MaskedDistribution)
    ):
        return _get_dist_name(fn.base_dist)
    return type(fn).__name__


def _get_abstract_trace(model, model_args, model_kwargs):
    def get_trace():
        # We use `init_to_sample` to get around ImproperUniform distribution,
        # which does not have `sample` method.
        subs_model = handlers.substitute(
            handlers.seed(model, 0),
            substitute_fn=init_to_sample,
        )
        trace = handlers.trace(subs_model).get_trace(*model_args, **model_kwargs)
        # Work around an issue where jax.eval_shape does not work
        # for distribution output (e.g. the function `lambda: dist.Normal(0, 1)`)
        # Here we will remove `fn` and store its name in the trace.
        for site in trace.values():
            if site["type"] == "sample":
                site["fn_name"] = _get_dist_name(site.pop("fn"))
        return PytreeTrace(trace)

    # We use eval_shape to avoid any array computation.
    return jax.eval_shape(get_trace).trace


def _get_log_probs(model, model_args, model_kwargs, sample):
    # Note: We use seed 0 for parameter initialization.
    with handlers.trace() as tr, handlers.seed(rng_seed=0), handlers.substitute(
        data=sample
    ):
        model(*model_args, **model_kwargs)
    return {
        name: site["fn"].log_prob(site["value"])
        for name, site in tr.items()
        if site["type"] == "sample"
    }


def get_dependencies(
    model: Callable,
    model_args: Optional[tuple] = None,
    model_kwargs: Optional[dict] = None,
) -> Dict[str, object]:
    r"""
    Infers dependency structure about a conditioned model.

    This returns a nested dictionary with structure like::

        {
            "prior_dependencies": {
                "variable1": {"variable1": set()},
                "variable2": {"variable1": set(), "variable2": set()},
                ...
            },
            "posterior_dependencies": {
                "variable1": {"variable1": {"plate1"}, "variable2": set()},
                ...
            },
        }

    where

    -   `prior_dependencies` is a dict mapping downstream latent and observed
        variables to dictionaries mapping upstream latent variables on which
        they depend to sets of plates inducing full dependencies.
        That is, included plates introduce quadratically many dependencies as
        in complete-bipartite graphs, whereas excluded plates introduce only
        linearly many dependencies as in independent sets of parallel edges.
        Prior dependencies follow the original model order.
    -   `posterior_dependencies` is a similar dict, but mapping latent
        variables to the latent or observed sits on which they depend in the
        posterior. Posterior dependencies are reversed from the model order.

    Dependencies elide ``pyro.deterministic`` sites and ``pyro.sample(...,
    Delta(...))`` sites.

    **Examples**

    Here is a simple example with no plates. We see every node depends on
    itself, and only the latent variables appear in the posterior::

        def model_1():
            a = numpyro.sample("a", dist.Normal(0, 1))
            numpyro.sample("b", dist.Normal(a, 1), obs=0.0)

        assert get_dependencies(model_1) == {
            "prior_dependencies": {
                "a": {"a": set()},
                "b": {"a": set(), "b": set()},
            },
            "posterior_dependencies": {
                "a": {"a": set(), "b": set()},
            },
        }

    Here is an example where two variables ``a`` and ``b`` start out
    conditionally independent in the prior, but become conditionally dependent
    in the posterior do the so-called collider variable ``c`` on which they
    both depend. This is called "moralization" in the graphical model
    literature::

        def model_2():
            a = numpyro.sample("a", dist.Normal(0, 1))
            b = numpyro.sample("b", dist.LogNormal(0, 1))
            c = numpyro.sample("c", dist.Normal(a, b))
            numpyro.sample("d", dist.Normal(c, 1), obs=0.)

        assert get_dependencies(model_2) == {
            "prior_dependencies": {
                "a": {"a": set()},
                "b": {"b": set()},
                "c": {"a": set(), "b": set(), "c": set()},
                "d": {"c": set(), "d": set()},
            },
            "posterior_dependencies": {
                "a": {"a": set(), "b": set(), "c": set()},
                "b": {"b": set(), "c": set()},
                "c": {"c": set(), "d": set()},
            },
        }

    Dependencies can be more complex in the presence of plates. So far all the
    dict values have been empty sets of plates, but in the following posterior
    we see that ``c`` depends on itself across the plate ``p``. This means
    that, among the elements of ``c``, e.g. ``c[0]`` depends on ``c[1]`` (this
    is why we explicitly allow variables to depend on themselves)::

        def model_3():
            with numpyro.plate("p", 5):
                a = numpyro.sample("a", dist.Normal(0, 1))
            numpyro.sample("b", dist.Normal(a.sum(), 1), obs=0.0)

        assert get_dependencies(model_3) == {
            "prior_dependencies": {
                "a": {"a": set()},
                "b": {"a": set(), "b": set()},
            },
            "posterior_dependencies": {
                "a": {"a": {"p"}, "b": set()},
            },
        }

    [1] S.Webb, A.Goliński, R.Zinkov, N.Siddharth, T.Rainforth, Y.W.Teh, F.Wood (2018)
        "Faithful inversion of generative models for effective amortized inference"
        https://dl.acm.org/doi/10.5555/3327144.3327229

    :param callable model: A model.
    :param tuple model_args: Optional tuple of model args.
    :param dict model_kwargs: Optional dict of model kwargs.
    :returns: A dictionary of metadata (see above).
    :rtype: dict
    """
    if model_args is None:
        model_args = ()
    if model_kwargs is None:
        model_kwargs = {}

    # Collect sites with tracked provenance.
    trace = _get_abstract_trace(model, model_args, model_kwargs)
    sample_sites = [msg for msg in trace.values() if is_sample_site(msg)]

    # Collect observations.
    observed = {msg["name"] for msg in sample_sites if msg["is_observed"]}
    plates = {
        msg["name"]: {f.name for f in msg["cond_indep_stack"]} for msg in sample_sites
    }

    # Find direct prior dependencies among latent and observed sites.
    samples = {
        name: ProvenanceArray(site["value"], frozenset({name}))
        for name, site in trace.items()
        if site["type"] == "sample" and not site["is_observed"]
    }
    sample_deps = get_provenance(
        eval_provenance(
            partial(_get_log_probs, model, model_args, model_kwargs), samples
        )
    )
    prior_dependencies = {n: {n: set()} for n in plates}  # no deps yet
    for i, downstream in enumerate(sample_sites):
        upstreams = [
            u
            for u in sample_sites[:i]
            if not u["is_observed"]
            if u["fn_name"] != "Unit"
        ]
        if not upstreams:
            continue
        provenance = sample_deps[downstream["name"]]
        for upstream in upstreams:
            u = upstream["name"]
            if u in provenance:
                d = downstream["name"]
                prior_dependencies[d][u] = set()

    # Next reverse dependencies and restrict downstream nodes to latent sites.
    posterior_dependencies = {n: {} for n in plates if n not in observed}
    for d, upstreams in prior_dependencies.items():
        for u, p in upstreams.items():
            if u not in observed:
                # Note the folowing reverses:
                # u is henceforth downstream and d is henceforth upstream.
                posterior_dependencies[u][d] = p.copy()

    # Moralize: add dependencies among latent variables in each Markov blanket.
    # This assumes all latents are eventually observed, at least indirectly.
    order = {msg["name"]: i for i, msg in enumerate(reversed(sample_sites))}
    for d, upstreams in prior_dependencies.items():
        upstreams = {u: p for u, p in upstreams.items() if u not in observed}
        for u1, p1 in upstreams.items():
            for u2, p2 in upstreams.items():
                if order[u1] <= order[u2]:
                    p12 = posterior_dependencies[u2].setdefault(u1, set())
                    p12 |= plates[u1] & plates[u2] - plates[d]
                    p12 |= plates[u2] & p1
                    p12 |= plates[u1] & p2

    return {
        "prior_dependencies": prior_dependencies,
        "posterior_dependencies": posterior_dependencies,
    }


# TODO: Move numpyro.contrib.render to here.
__all__ = [
    "get_dependencies",
]
