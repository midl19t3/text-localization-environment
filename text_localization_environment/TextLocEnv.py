import gym
import random
import numpy as np
from gym import spaces
from gym.utils import seeding
from chainer.backends import cuda
from PIL import Image, ImageDraw
from PIL.Image import LANCZOS, MAX_IMAGE_PIXELS
from .ImageMasker import ImageMasker
from .transformer import create_bbox_transformer
from .utils import scale_bboxes


class TextLocEnv(gym.Env):
    metadata = {'render.modes': ['human', 'rgb_array', 'box']}

    # Base reward for trigger action
    ETA_TRIGGER = 70.0
    # Base reward for termination action
    ETA_TERMINATION = 10.0
    # Penalty substracted from trigger reward
    DURATION_PENALTY = 0.03

    # Probability for masking a bounding box in a new observation (applied during premasking)
    P_MASK = 0.5

    def __init__(self, image_paths, true_bboxes,
        playout_episode=False, premasking=True, mode='train',
        max_steps_per_image=200, seed=None, bbox_scaling=0.125,
        bbox_transformer='base', has_termination_action=True,
        ior_marker_type='cross', history_length=10
    ):
        """
        :param image_paths: The paths to the individual images
        :param true_bboxes: The true bounding boxes for each image
        :type image_paths: String or list
        :type true_bboxes: numpy.ndarray
        """
        # Determines whether the agent is training or testing
        # Optimizations can be applied during training that are not allowed for testing
        self.mode = mode
        # Factor for scaling all bounding boxes relative to their size
        self.bbox_scaling = bbox_scaling
        # Whether IoR markers will be placed upfront after loading the image
        self.premasking = premasking
        # Whether an episode terminates after a single trigger or is played out until the end
        self.playout_episode = playout_episode
        # Episodes will be terminated automatically after reaching max steps
        self.max_steps_per_image = max_steps_per_image
        # Whether a termination action should be provided in the action set
        self.has_termination_action = has_termination_action
        # The type of IoR marker to be used when masking trigger regions
        self.ior_marker_type = ior_marker_type
        # Length of history in state & agent model
        self.history_length = history_length

        # Initialize action space
        self.bbox_transformer = create_bbox_transformer(bbox_transformer)
        self.action_space = spaces.Discrete(len(self.action_set))
        # 224*224*3 (RGB image) + 9 * 10 (on-hot-enconded history) = 150618
        self.observation_space = spaces.Tuple([
            spaces.Box(low=0, high=256, shape=(224, 224, 3)),
            spaces.Box(low=0, high=1, shape=(self.history_length, len(self.action_set)))
        ])

        # Initialize dataset
        if type(image_paths) is not list:
            image_paths = [image_paths]
        self.image_paths = image_paths
        self.true_bboxes = [[TextLocEnv.to_standard_box(b) for b in bboxes] for bboxes in true_bboxes]

        # For registering a handler that will be executed once after a step
        self.post_step_handler = None

        # Episode-specific

        # Image for the current episode
        self.episode_image = None
        # Ground truth bounding boxes for the current episode image
        self.episode_true_bboxes = None
        # Predicted bounding boxes for the current episode image
        self.episode_pred_bboxes = None
        # IoU values for each trigger in the current episode
        self.episode_trigger_ious = None
        # List of indices of masked bounding boxes for the current episode image
        self.episode_masked_indices = []
        # Number of trigger actions used so far
        self.num_triggers_used = 0

        self.seed(seed=seed)
        self.reset()

    @property
    def action_set(self):
        n_actions = len(self.bbox_transformer.action_set)
        actions = {**self.bbox_transformer.action_set}
        actions[n_actions] = self.trigger
        if self.has_termination_action:
            actions[n_actions + 1] = self.terminate
        return actions

    @property
    def bbox(self):
        # The agent's current window represented as [x0, y0, x1, y1]
        return self.bbox_transformer.bbox

    def seed(self, seed=None):
        # Note: Please use np_random object instead of np.random
        if seed is not None:
            np.random.seed(seed)
            random.seed(seed)
        self.np_random, seed = seeding.np_random(seed)
        return [seed]

    def step(self, action):
        """Execute an action and return
            state - the next state,
            reward - the reward,
            done - whether a terminal state was reached,
            info - any additional info"""
        assert self.action_space.contains(action), "%r (%s) is an invalid action" % (action, type(action))

        self.current_step += 1

        self.action_set[action]()

        reward = self.calculate_reward(action)
        self.max_iou = max(self.iou, self.max_iou)

        self.history.insert(0, self.to_one_hot(action))
        self.history.pop()

        self.state = self.compute_state()

        # Execute and remove any registered post-step handler
        if self.post_step_handler is not None:
            self.post_step_handler()
            self.post_step_handler = None

        # Terminate episode after reaching step limit (if set)
        # Prevents the agent from running into an infinite loop
        if self.max_steps_per_image != -1 and self.current_step >= self.max_steps_per_image:
            self.done = True

        return self.state, reward, self.done, {}

    def calculate_reward(self, action):
        reward = 0

        if self.has_termination_action and self.is_termination(action):
            pct_triggers_used = self.num_triggers_used / len(self.episode_true_bboxes)
            if pct_triggers_used != 1.0:
                return -1 * self.ETA_TERMINATION
            else:
                return self.ETA_TERMINATION + (self.current_step * self.DURATION_PENALTY)

        if self.is_trigger(action):
            reward = self.ETA_TRIGGER * self.iou - (self.current_step * self.DURATION_PENALTY)
        else:
            self.iou = self.compute_best_iou()

        return reward

    def create_empty_history(self):
        flat_history = np.repeat([False], self.history_length * self.action_space.n)
        history = flat_history.reshape((self.history_length, self.action_space.n))

        return history.tolist()

    @staticmethod
    def to_standard_box(bbox):
        """
        Transforms a given bounding box into a standardized representation.

        :param bbox: Bounding box given as [(x0, y0), (x1, y1)] or [x0, y0, x1, y1]
        :return: Bounding box represented as [x0, y0, x1, y1]
        """
        from typing import Iterable
        if isinstance(bbox[0], Iterable):
            bbox = [xy for p in bbox for xy in p]
        return bbox

    def create_ior_mark(self, bbox):
        """
        Creates an IoR (inhibition of return) mark that crosses out the given bounding box.
        This is necessary to find multiple objects within one image

        :param bbox: Bounding box given as [(x0, y0), (x1, y1)] or [x0, y0, x1, y1]
        """
        bbox = self.to_standard_box(bbox)
        masker = ImageMasker(self.episode_image, bbox, self.ior_marker_type)
        self.episode_image = masker.mask()

    @property
    def episode_true_bboxes_unmasked(self):
        """
        Returns the bounding boxes in the current episode image that are not masked.
        """
        bboxes_unmasked = []

        for index, bbox in enumerate(self.episode_true_bboxes):
            is_masked = index in self.episode_masked_indices
            if not is_masked:
                bboxes_unmasked.append(bbox)

        return bboxes_unmasked

    def compute_best_iou(self):
        max_iou = 0

        # Only consider boxes that have not been masked yet
        # Ensures that the agent is not rewarded for visiting the same location
        for box in self.episode_true_bboxes_unmasked:
            max_iou = max(max_iou, self.compute_iou(box))

        return max_iou

    def compute_iou(self, other_bbox):
        """Computes the intersection over union of the argument and the current bounding box."""
        intersection = self.compute_intersection(other_bbox)

        area_1 = (self.bbox[2] - self.bbox[0]) * (self.bbox[3] - self.bbox[1])
        area_2 = (other_bbox[2] - other_bbox[0]) * (other_bbox[3] - other_bbox[1])
        union = area_1 + area_2 - intersection

        return intersection / union

    def compute_intersection(self, other_bbox):
        left = max(self.bbox[0], other_bbox[0])
        top = max(self.bbox[1], other_bbox[1])
        right = min(self.bbox[2], other_bbox[2])
        bottom = min(self.bbox[3], other_bbox[3])

        if right < left or bottom < top:
            return 0

        return (right - left) * (bottom - top)

    def trigger(self):
        self.num_triggers_used += 1
        self.episode_pred_bboxes.append(self.bbox)
        # IoU values are only updated after trigger action is executed
        # Therefore we need to track them lazily
        self.post_step_handler = self._register_trigger_iou

        if not self.playout_episode:
            # Terminate episode after first trigger action
            self.done = True
            return

        if self.mode == 'train':
            if len(self.episode_true_bboxes_unmasked) > 0:
                index, bbox = self.closest_unmasked_true_bbox()
                self.create_ior_mark(bbox)
                self.episode_masked_indices.append(index)
        else:
            self.create_ior_mark(self.bbox)

        self.bbox_transformer.reset(self.episode_image.width, self.episode_image.height)

    def _register_trigger_iou(self):
        self.episode_trigger_ious.append(self.iou)

    def closest_unmasked_true_bbox(self):
        max_iou = None
        best_box = None
        best_box_index = None

        for index, box in enumerate(self.episode_true_bboxes):
            if index in self.episode_masked_indices:
                continue
            iou = self.compute_iou(box)
            if not max_iou or iou > max_iou:
                max_iou = iou
                best_box = box
                best_box_index = index

        return (best_box_index, best_box)

    def terminate(self):
        """Termination action to be used when all text instanced have been found."""
        self.done = True

    def reset(self, image_index=None):
        """Reset the environment to its initial state (the bounding box covers the entire image)"""
        self.history = self.create_empty_history()
        if self.episode_image is not None:
            self.episode_image.close()

        if image_index is None:
            # Pick random next image if not specified otherwise
            image_index = self.np_random.randint(len(self.image_paths))
        self.episode_image = Image.open(self.image_paths[image_index])
        self.episode_true_bboxes = self.true_bboxes[image_index]

        # Scale up/down by bounding boxes relative to their size
        if self.bbox_scaling is not None and self.bbox_scaling != 1.0:
            self.episode_true_bboxes = scale_bboxes(
                self.episode_true_bboxes, self.episode_image.size,
                self.bbox_scaling
            )

        if self.episode_image.mode != 'RGB':
            self.episode_image = self.episode_image.convert('RGB')

        self.episode_masked_indices = []

        # Mask bounding boxes randomly with probability P_MASK
        if self.mode == 'train' and self.premasking:
            num_unmasked = self.episode_num_true_bboxes
            for idx, box in enumerate(self.episode_true_bboxes):
                # Ensure at least 0 non-masked instance per observation
                # -> possibly all texts are masked to train NextImageTrigger
                mask_rand = self.np_random.random()
                min_unmasked = 0 if self.has_termination_action else 1
                if num_unmasked > min_unmasked and mask_rand <= self.P_MASK:
                    self.create_ior_mark(box)
                    self.episode_masked_indices.append(idx)
                    num_unmasked -= 1

        self.episode_pred_bboxes = []
        self.episode_trigger_ious = []
        self.num_triggers_used = 0
        self.current_step = 0
        self.bbox_transformer.reset(self.episode_image.width, self.episode_image.height)
        self.state = self.compute_state()
        self.done = False
        self.iou = self.compute_best_iou()
        self.max_iou = self.iou

        return self.state

    def render(self, mode='human', return_as_file=False, include_true_bboxes=False):
        """Render the current state"""
        image = self.episode_image
        if include_true_bboxes:
            image = self.episode_image_with_true_bboxes

        if mode == 'human':
            copy = image.copy()
            draw = ImageDraw.Draw(copy)
            draw.rectangle(self.bbox.tolist(), outline=(255, 255, 255))
            if return_as_file:
                return copy
            copy.show()
            copy.close()
        elif mode is 'box':
            # Renders what the agent currently sees
            # i.e. the section of the image covered by the agent's current window (warped to standard size)
            warped = self.get_warped_bbox_contents()
            if return_as_file:
                return warped
            warped.show()
            warped.close()
        elif mode is 'rgb_array':
            copy = image.copy()
            draw = ImageDraw.Draw(copy)
            draw.rectangle(self.bbox.tolist(), outline=(255, 255, 255))
            return np.array(copy)
        else:
            super(TextLocEnv, self).render(mode=mode)

    def get_warped_bbox_contents(self):
        cropped = self.episode_image.crop(self.bbox)
        return cropped.resize((224, 224), LANCZOS)

    def compute_state(self):
        warped = self.get_warped_bbox_contents()
        return (np.array(warped, dtype=np.float32), np.array(self.history))

    def to_one_hot(self, action):
        line = np.zeros(self.action_space.n, np.bool)
        line[action] = 1

        return line

    @property
    def episode_image_with_true_bboxes(self, true_bbox_color=(255, 0, 0)):
        if not self.episode_true_bboxes:
            return self.episode_image

        copy = self.episode_image.copy()
        draw = ImageDraw.Draw(copy)
        for box in self.episode_true_bboxes:
            draw.rectangle(box, outline=true_bbox_color)
        return copy

    @property
    def episode_num_true_bboxes(self):
        """Number of bounding boxes available in the current episode image."""
        if not self.episode_true_bboxes:
            return None
        return len(self.episode_true_bboxes)

    def is_trigger(self, action):
        return self.action_set[action] == self.trigger

    def is_termination(self, action):
        return self.action_set[action] == self.terminate
