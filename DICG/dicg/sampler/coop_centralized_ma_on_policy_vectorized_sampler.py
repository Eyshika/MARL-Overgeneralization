"""BatchSampler which uses VecEnvExecutor to run multiple environments."""
import itertools
import pickle
import time

from dowel import logger, tabular
import numpy as np

from garage.experiment import deterministic
from garage.misc import tensor_utils
from garage.misc.prog_bar_counter import ProgBarCounter
from garage.sampler.batch_sampler import BatchSampler
from garage.sampler.stateful_pool import singleton_pool
from garage.sampler.utils import truncate_paths
from garage.sampler.vec_env_executor import VecEnvExecutor


class CoopCentralizedMAOnPolicyVectorizedSampler(BatchSampler):
    """BatchSampler which uses VecEnvExecutor to run multiple environments.

    Args:
        algo (garage.np.algos.RLAlgorithm): An algorithm instance.
        env (garage.envs.GarageEnv): An environement instance.
        n_envs (int): Number of environment instances to setup.
            This parameter has effect on sampling performance.

    """

    def __init__(self, algo, env, n_envs=None):
        if n_envs is None:
            n_envs = singleton_pool.n_parallel * 4
        super().__init__(algo, env)
        self._n_envs = n_envs
        self._n_agents = env.n_agents

        self._vec_env = None
        self._env_spec = self.env.spec

    def start_worker(self):
        """Start workers."""
        n_envs = self._n_envs
        if self.env.pickleable:
            envs = [pickle.loads(pickle.dumps(self.env)) for _ in range(n_envs)]
        else:
            envs = [self.env for _ in range(n_envs)]

        # Deterministically set environment seeds based on the global seed.
        seed0 = deterministic.get_seed()
        if seed0 is not None:
            for (i, e) in enumerate(envs):
                e.seed(seed0 + i)

        self._vec_env = VecEnvExecutor(
            envs=envs, max_path_length=self.algo.max_path_length)

    def shutdown_worker(self):
        """Shutdown workers."""
        self._vec_env.close()

    # pylint: disable=too-many-statements
    def obtain_samples(self, itr, batch_size=None, whole_paths=True):
        """Sample the policy for new trajectories.

        Args:
            itr (int): Iteration number.
            batch_size (int): Number of samples to be collected. If None,
                it will be default [algo.max_path_length * n_envs].
            whole_paths (bool): Whether return all the paths or not. True
                by default. It's possible for the paths to have total actual
                sample size larger than batch_size, and will be truncated if
                this flag is true.

        Returns:
            list[dict]: Sample paths.

        Note:
            Each path is a dictionary, with keys and values as following:
                * observations: numpy.ndarray with shape [Batch, *obs_dims]
                * actions: numpy.ndarray with shape [Batch, *act_dims]
                * rewards: numpy.ndarray with shape [Batch, ]
                * env_infos: A dictionary with each key representing one
                  environment info, value being a numpy.ndarray with shape
                  [Batch, ?]. One example is "ale.lives" for atari
                  environments.
                * agent_infos: A dictionary with each key representing one
                  agent info, value being a numpy.ndarray with shape
                  [Batch, ?]. One example is "prev_action", which is used
                  for recurrent policy as previous action input, merged with
                  the observation input as the state input.
                * dones: numpy.ndarray with shape [Batch, ]

        """
        logger.log('Obtaining samples for iteration %d...' % itr)

        if not batch_size:
            batch_size = self.algo.max_path_length * self._n_envs

        paths = []
        n_samples = 0
        if hasattr(self.env, 'curriculum_learning'):
            obses = self._vec_env.reset(itr)
        else:
            obses = self._vec_env.reset()
        dones = np.asarray([True] * self._vec_env.num_envs)
        running_paths = [None] * self._vec_env.num_envs

        pbar = ProgBarCounter(batch_size)
        policy_time = 0
        env_time = 0
        process_time = 0

        policy = self.algo.policy

        while n_samples < batch_size:
            t = time.time()
            policy.reset(dones)

            avail_actions = np.array(
                [e.get_avail_actions() for e in self._vec_env.envs])

            actions, agent_infos = policy.get_actions(obses, avail_actions)

            policy_time += time.time() - t
            t = time.time()
            if hasattr(self.env, 'curriculum_learning'):
                next_obses, rewards, dones, env_infos = self._vec_env.step(actions, itr)
            else:
                next_obses, rewards, dones, env_infos = self._vec_env.step(actions)
            env_time += time.time() - t
            t = time.time()
            agent_infos = tensor_utils.split_tensor_dict_list(agent_infos)
            env_infos = tensor_utils.split_tensor_dict_list(env_infos)
            if env_infos is None:
                env_infos = [dict() for _ in range(self._vec_env.num_envs)]
            if agent_infos is None:
                agent_infos = [dict() for _ in range(self._vec_env.num_envs)]
            coops = [env_info['coop'] for env_info in env_infos]
            for idx, observation, avail_action, action, reward, env_info, \
                agent_info, done, coop in zip(itertools.count(), obses, avail_actions,
                actions, rewards, env_infos, agent_infos, dones, coops):
                if running_paths[idx] is None:
                    running_paths[idx] = dict(observations=[],
                                              avail_actions=[],
                                              actions=[],
                                              rewards=[],
                                              env_infos=[],
                                              agent_infos=[],
                                              dones=[],
                                              coops=[])
                running_paths[idx]['observations'].append(observation)
                running_paths[idx]['avail_actions'].append(np.asarray(avail_action))
                running_paths[idx]['actions'].append(action)
                running_paths[idx]['rewards'].append(reward)
                running_paths[idx]['env_infos'].append(env_info)
                running_paths[idx]['agent_infos'].append(agent_info)
                running_paths[idx]['dones'].append(done)
                running_paths[idx]['coops'].append(coop)
                if done:
                    _obs = np.asarray(running_paths[idx]['observations'])
                    _actions = np.asarray(running_paths[idx]['actions'])
                    _avail_actions = np.asarray(running_paths[idx]['avail_actions'])
                    paths.append(
                        dict(observations=_obs,
                             actions=_actions,
                             avail_actions=_avail_actions,
                             rewards=np.asarray(running_paths[idx]['rewards']),
                             env_infos=tensor_utils.stack_tensor_dict_list(
                                 running_paths[idx]['env_infos']),
                             agent_infos=tensor_utils.stack_tensor_dict_list(
                                 running_paths[idx]['agent_infos']),
                             dones=np.asarray(running_paths[idx]['dones']),
                             coops=np.asarray(running_paths[idx]['coops'])))
                    n_samples += len(running_paths[idx]['rewards'] * self._n_agents)
                    running_paths[idx] = None

            process_time += time.time() - t
            pbar.inc(self._n_envs * self._n_agents)
            # print(n_samples)
            obses = next_obses

        pbar.stop()

        tabular.record('PolicyExecTime', policy_time)
        tabular.record('EnvExecTime', env_time)
        tabular.record('ProcessExecTime', process_time)

        return paths if whole_paths else truncate_paths(paths, batch_size)
