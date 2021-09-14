from gym import core, spaces
from dm_control import suite
from dm_env import specs
import numpy as np
from copy import deepcopy


def filter_dict_by_keys(dictionary, keys_to_exclude):
    dictionary_copy = deepcopy(dictionary)
    for k in keys_to_exclude:
        if k not in dictionary_copy:
            raise ValueError("To exclude a key, it must exist.")
        del dictionary_copy[k]
    return dictionary_copy


def _spec_to_box(spec):
    def extract_min_max(s):
        assert s.dtype == np.float64 or s.dtype == np.float32
        dim = np.int(np.prod(s.shape))
        if type(s) == specs.Array:
            bound = np.inf * np.ones(dim, dtype=np.float32)
            return -bound, bound
        elif type(s) == specs.BoundedArray:
            zeros = np.zeros(dim, dtype=np.float32)
            return s.minimum + zeros, s.maximum + zeros

    mins, maxs = [], []
    for s in spec:
        mn, mx = extract_min_max(s)
        mins.append(mn)
        maxs.append(mx)
    low = np.concatenate(mins, axis=0)
    high = np.concatenate(maxs, axis=0)
    assert low.shape == high.shape
    return spaces.Box(low, high, dtype=np.float32)


def _flatten_obs(obs):
    obs_pieces = []
    for v in obs.values():
        flat = np.array([v]) if np.isscalar(v) else v.ravel()
        obs_pieces.append(flat)
    return np.concatenate(obs_pieces, axis=0)


class DMCWrapper(core.Env):
    def __init__(
        self,
        domain_name,
        task_name,
        keys_to_exclude=[],
        task_kwargs=None,
        visualize_reward={},
        from_pixels=False,
        height=84,
        width=84,
        camera_id=0,
        frame_skip=1,
        environment_kwargs=None,
        channels_first=True,
        track_prev_action=False
    ):
        assert 'random' in task_kwargs, 'please specify a seed, for deterministic behaviour'
        self._from_pixels = from_pixels
        self._height = height
        self._width = width
        self._camera_id = camera_id
        self._frame_skip = frame_skip
        self._channels_first = channels_first
        self._track_prev_action = track_prev_action

        # create task
        self._env = suite.load(
            domain_name=domain_name,
            task_name=task_name,
            task_kwargs=task_kwargs,
            visualize_reward=visualize_reward,
            environment_kwargs=environment_kwargs
        )

        # true and normalized action spaces
        self._true_action_space = _spec_to_box([self._env.action_spec()])
        self._norm_action_space = spaces.Box(
            low=-1.0,
            high=1.0,
            shape=self._true_action_space.shape,
            dtype=np.float32
        )

        # create observation space
        if from_pixels:
            assert not track_prev_action and len(keys_to_exclude) == 0
            shape = [3, height, width] if channels_first else [height, width, 3]
            self._observation_space = spaces.Box(
                low=0, high=255, shape=shape, dtype=np.uint8
            )
        else:
            if not self._track_prev_action:
                obs_spec = filter_dict_by_keys(self._env.observation_spec(), keys_to_exclude)
                self._observation_space = _spec_to_box(obs_spec.values())
            else:
                obs_spec = filter_dict_by_keys(self._env.observation_spec(), keys_to_exclude)
                obs_spec["action"] = self._env.action_spec()
                self._observation_space = _spec_to_box(obs_spec.values())

        self._state_space = _spec_to_box(obs_spec.values())
        
        self.current_state = None

        # set seed
        self.seed(seed=task_kwargs.get('random', 1))

        self.keys_to_exclude = keys_to_exclude

    def __getattr__(self, name):
        return getattr(self._env, name)

    def _get_obs(self, time_step):
        if self._from_pixels:
            obs = self.render(
                height=self._height,
                width=self._width,
                camera_id=self._camera_id
            )
            if self._channels_first:
                obs = obs.transpose(2, 0, 1).copy()
        else:
            obs = _flatten_obs(filter_dict_by_keys(time_step.observation, self.keys_to_exclude))
        return obs

    def _convert_action(self, action):
        action = action.astype(np.float64)
        true_delta = self._true_action_space.high - self._true_action_space.low
        norm_delta = self._norm_action_space.high - self._norm_action_space.low
        action = (action - self._norm_action_space.low) / norm_delta
        action = action * true_delta + self._true_action_space.low
        action = action.astype(np.float32)
        return action

    @property
    def observation_space(self):
        return self._observation_space

    @property
    def state_space(self):
        return self._state_space

    @property
    def action_space(self):
        return self._norm_action_space

    def seed(self, seed):
        self._true_action_space.seed(seed)
        self._norm_action_space.seed(seed)
        self._observation_space.seed(seed)

    def step(self, action):

        assert self._norm_action_space.contains(action)
        action = self._convert_action(action)
        assert self._true_action_space.contains(action)
        reward = 0
        extra = {'internal_state': self._env.physics.get_state().copy()}

        for _ in range(self._frame_skip):
            time_step = self._env.step(action)
            reward += time_step.reward or 0
            done = time_step.last()
            if done:
                break

        obs = self._get_obs(time_step)
        self.current_state = _flatten_obs(filter_dict_by_keys(time_step.observation, self.keys_to_exclude))

        extra['discount'] = time_step.discount
        if self._track_prev_action:
            return np.concatenate([obs, action]), reward, done, extra
        else:
            return obs, reward, done, extra

    def reset(self):
        time_step = self._env.reset()
        self.current_state = _flatten_obs(filter_dict_by_keys(time_step.observation, self.keys_to_exclude))
        obs = self._get_obs(time_step)
        if self._track_prev_action:
            return np.concatenate([obs, np.zeros(*self._true_action_space.shape)])
        else:
            return obs

    def render(self, mode='rgb_array', height=None, width=None, camera_id=0):
        assert mode == 'rgb_array', 'only support rgb_array mode, given %s' % mode
        height = height or self._height
        width = width or self._width
        camera_id = camera_id or self._camera_id
        return self._env.physics.render(
            height=height, width=width, camera_id=camera_id
        )
