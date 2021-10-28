"""
HMM Model Classes
=================

Module defining model behavior for Hidden Markov Models (HMMs).
"""
from typing import Any

from ssm.distributions.discrete_chain import StationaryDiscreteChain
Array = Any

import jax.numpy as np
import jax.random as jr
import jax.scipy.special as spsp
from jax import vmap, lax
from jax.tree_util import register_pytree_node_class

from tensorflow_probability.substrates import jax as tfp

from ssm.base import SSM
from ssm.inference.em import em
from ssm.hmm.posterior import hmm_expected_states, HMMPosterior
from ssm.utils import Verbosity, format_dataset, one_hot


class HMM(SSM):

    def __init__(self, num_states: int,
                 initial_distribution: tfp.distributions.Categorical,
                 transition_distribution: tfp.distributions.Categorical,
                 emission_distribution: tfp.distributions.Distribution,
                 initial_distribution_prior: tfp.distributions.Dirichlet=None,
                 transition_distribution_prior: tfp.distributions.Dirichlet=None,
                 emission_distribution_prior: tfp.distributions.Distribution=None,
                 ):
        """Class for Hidden Markov Model (HMM).

        Args:
            num_states (int): Number of discrete latent states.
            initial_distribution (tfp.distributions.Categorical): The distribution over the initial state.
            transition_distribution (tfp.distributions.Categorical): The transition distribution.
        """
        self.num_states = num_states
        self._initial_distribution = initial_distribution
        self._transition_distribution = transition_distribution
        self._emission_distribution = emission_distribution

        # Initialize uniform priors unless otherwise specified
        if initial_distribution_prior is None:
            initial_distribution_prior = \
                tfp.distributions.Dirichlet(1.1 * np.ones(num_states))
        self._initial_distribution_prior = initial_distribution_prior

        if transition_distribution_prior is None:
            transition_distribution_prior = \
                tfp.distributions.Dirichlet(1.1 * np.ones((num_states, num_states)))
        self._transition_distribution_prior = transition_distribution_prior

        # Subclasses can initialize in their constructors this as necessary
        self._emission_distribution_prior = emission_distribution_prior

    def tree_flatten(self):
        children = (self._initial_distribution,
                    self._transition_distribution,
                    self._emission_distribution,
                    self._initial_distribution_prior,
                    self._transition_distribution_prior,
                    self._emission_distribution_prior)
        aux_data = self.num_states
        return children, aux_data

    @classmethod
    def tree_unflatten(cls, aux_data, children):
        return cls(aux_data, *children)

    def initial_distribution(self):
        return self._initial_distribution

    def dynamics_distribution(self, state):
        return self._transition_distribution[state]

    def emissions_distribution(self, state):
        return self._emission_distribution[state]

    @property
    def transition_matrix(self):
        return self._transition_distribution.probs_parameter()

    ### Methods for posterior inference
    @format_dataset
    def initialize(self, dataset, key, method="kmeans"):
        """
        Initialize the model parameters by performing an M-step with state assignments
        determined by the specified method (random or kmeans).
        """
        # initialize assignments and perform one M-step
        num_states = self.num_states
        if method.lower() == "random":
            # randomly assign datapoints to clusters
            assignments = jr.choice(key, self.num_states, dataset.shape[:-1])

        elif method.lower() == "kmeans":
            # cluster the data with kmeans
            print("initializing with kmeans")
            from sklearn.cluster import KMeans
            km = KMeans(num_states)
            flat_dataset = dataset.reshape(-1, dataset.shape[-1])
            assignments = km.fit_predict(flat_dataset).reshape(dataset.shape[:-1])

        else:
            raise Exception("Observations.initialize: "
                "Invalid initialize method: {}".format(method))

        Ez = one_hot(assignments, self.num_states)
        dummy_posteriors = HMMPosterior(None, Ez, None)
        self._m_step_emission_distribution(dataset, dummy_posteriors)

    def _log_initial_state_probabilities(self, data: Array):
        lp = self._initial_distribution.logits_parameter()
        lp -= spsp.logsumexp(lp, axis=-1, keepdims=True)
        return lp


    def _log_transition_probabilities(self, data: Array):
        log_transition_matrix = self._transition_distribution.logits_parameter()
        log_transition_matrix -= spsp.logsumexp(log_transition_matrix, axis=1, keepdims=True)
        return log_transition_matrix

    def _log_likelihoods(self, data: Array):
        return vmap(lambda k:
            vmap(lambda x: self.emissions_distribution(k).log_prob(x))(data)
            )(np.arange(self.num_states)).T

    def infer_posterior(self, data):
        return StationaryDiscreteChain(
            self._log_initial_state_probabilities(data),
            self._log_likelihoods(data),
            self._log_transition_probabilities(data))

    def marginal_likelihood(self, data, posterior=None):
        if posterior is None:
            posterior = self.infer_posterior(data)

        dummy_states = np.zeros(data.shape[0], dtype=int)
        return self.log_probability(dummy_states, data) - posterior.log_prob(dummy_states)

    ### EM: Operates on batches of data (aka datasets) and posteriors
    def _m_step_initial_distribution(self, posteriors):
        stats = np.sum(posteriors.expected_states[:, 0, :], axis=0)
        stats += self._initial_distribution_prior.concentration
        conditional = tfp.distributions.Dirichlet(concentration=stats)
        self._initial_distribution = tfp.distributions.Categorical(probs=conditional.mode())

    def _m_step_transition_distribution(self, posteriors):
        stats = np.sum(posteriors.expected_transitions, axis=0)
        stats += self._transition_distribution_prior.concentration
        conditional =  tfp.distributions.Dirichlet(concentration=stats)
        self._transition_distribution = tfp.distributions.Categorical(probs=conditional.mode())

    def _m_step_emission_distribution(self, dataset, posteriors):
        # TODO: We could do gradient ascent on the expected log likelihood
        raise NotImplementedError

    def m_step(self, dataset, posteriors):
        self._m_step_initial_distribution(posteriors)
        self._m_step_transition_distribution(posteriors)
        self._m_step_emission_distribution(dataset, posteriors)

    @format_dataset
    def fit(self, dataset,
            method="em",
            num_iters=100,
            tol=1e-4,
            initialization_method="kmeans",
            key=None,
            verbosity=Verbosity.DEBUG):
        """
        Fit the parameters of the HMM using the specified method.

        Args:

        dataset: see `help(HMM)` for details.

        method: specification of how to fit the data.  Must be one
        of the following strings:
        - em

        initialization_method: optional method name ("kmeans" or "random")
        indicating how to initialize the model before fitting.

        key: jax.PRNGKey for random initialization and/or fitting

        verbosity: specify how verbose the print-outs should be.  See
        `ssm.util.Verbosity`.
        """
        model = self
        kwargs = dict(num_iters=num_iters, tol=tol, verbosity=verbosity)

        if initialization_method is not None:
            if verbosity >= Verbosity.LOUD : print("Initializing...")
            self.initialize(dataset, key, method=initialization_method)
            if verbosity >= Verbosity.LOUD: print("Done.", flush=True)

        if method == "em":
            log_probs, model, posteriors = em(model, dataset, **kwargs)
        else:
            raise ValueError(f"Method {method} is not recognized/supported.")

        return log_probs, model, posteriors


@register_pytree_node_class
class AutoregressiveHMM(HMM):
    """
    TODO
    """
    @property
    def emission_dim(self):
        return self._emission_distribution.data_dimension

    @property
    def num_lags(self):
        return self._emission_distribution.covariate_dimension // self._emission_distribution.data_dimension

    def log_probability(self, states, data, prev_emissions=None):
        """
        Computes the log joint probability of a set of states and data (observations).

        .. math::
            \log p(x, y) = \log p(x_1) + \sum_{t=1}^{T-1} \log p(x_{t+1} | x_t) + \sum_{t=1}^{T} \log p(y_t | x_t)

        Args:
            states: An array of latent states (:math:`x_{1:T}`).
            data: An array of the observed data (:math:`y_{1:T}`).

        Returns:
            lp:
                The joint log probability of the provided states and data.
        """
        if prev_emissions is None:
            prev_emissions = np.zeros((self.num_lags, self.emission_dim))

        lp = 0
        lp += self.initial_distribution().log_prob(states[0])
        lp += self.emissions_distribution(states[0]).log_prob(data[0], covariates=prev_emissions.ravel())

        def _step(carry, args):
            prev_state, prev_emissions, lp = carry
            state, emission = args
            lp += self.dynamics_distribution(prev_state).log_prob(state)
            lp += self.emissions_distribution(state).log_prob(emission, covariates=prev_emissions.ravel())
            new_prev_emissions = np.row_stack([prev_emissions[1:], emission])
            return (state, new_prev_emissions, lp), None

        initial_carry = (states[0], np.row_stack([prev_emissions[1:], data[0]]), lp)
        (_, _, lp), _ = lax.scan(_step, initial_carry, (states[1:], data[1:]))
        return lp


    def sample(self, key, num_steps: int, initial_state=None, num_samples=1, prev_emissions=None):
        """
        Sample from the joint distribution defined by the state space model.

        .. math::
            x, y \sim p(x, y)

        Args:
            key (PRNGKey): A JAX pseudorandom number generator key.
            num_steps (int): Number of steps for which to sample.
            initial_state: Optional state on which to condition the sampled trajectory.
                Default is None which samples the intial state from the initial distribution.
            prev_emissions: Optional initial emissions to start the autoregressive model.

        Returns:
            states: A ``(timesteps,)`` array of the state value across time (:math:`x`).
            emissions: A ``(timesteps, obs_dim)`` array of the observations across time (:math:`y`).

        """

        def _sample(key):
            if initial_state is None:
                key1, key = jr.split(key, 2)
                state = self.initial_distribution().sample(seed=key1)
            else:
                state = initial_state

            if prev_emissions is None:
                history = np.zeros((self.num_lags, self.emission_dim))
            else:
                history = prev_emissions

            def _step(carry, key):
                history, state = carry
                key1, key2 = jr.split(key, 2)
                emission = self.emissions_distribution(state).sample(seed=key1, covariates=history.ravel())
                next_state = self.dynamics_distribution(state).sample(seed=key2)
                next_history = np.row_stack([history[1:], emission])
                return (next_history, next_state), (state, emission)

            keys = jr.split(key, num_steps)
            _, (states, emissions) = lax.scan(_step, (history, state), keys)
            return states, emissions

        if num_samples > 1:
            batch_keys = jr.split(key, num_samples)
            states, emissions = vmap(_sample)(batch_keys)
        else:
            states, emissions = _sample(key)

        return states, emissions

    def _log_likelihoods(self, data: Array):
        def _compute_ll(x, y):
            ll = self._emission_distribution.log_prob(y, covariates=x.ravel())
            new_x = np.row_stack([x[1:], y])
            return new_x, ll

        _, log_likelihoods = lax.scan(_compute_ll, np.zeros((self.num_lags, self.emission_dim)), data)

        # Ignore likelihood of the first bit of data since we don't have a prefix
        log_likelihoods = log_likelihoods.at[:self.num_lags].set(0.0)
        return log_likelihoods
