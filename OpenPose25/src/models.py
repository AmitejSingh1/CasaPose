from time import time

import numpy as np

import tensorflow as tf
import tensorflow_probability as tfb
import tensorflow.keras as tfk
import tensorflow.keras.layers as tfkl

import cv2

from .utils import Detection, BoundingBox, Drawer


class OpenPoseV2:

    """OpenPose body_25 version.

    :get_detections
            Given a RGB(0, 255) image, model resize-pads it to (model_config.input_res, model_config.input_res),
         and makes inference on it, then re-maps the detections to original size and returns the detections as a
         list of Detection objects

    Note that the resize function is based on tensorflow's resize_and_pad with pad values = hyper_config.pad_value.
    """

    def __init__(self,
                 model_config,
                 hyper_config,
                 do_pad_resizing=False,
                 verbose=True):
        self.hyper_config = hyper_config
        self.input_res = model_config.input_res
        self.openpose_model = InterMediateOpenPose(model_config=model_config,
                                                   hyper_config=hyper_config,
                                                   verbose=verbose)
        # self.openpose_model = FastOpenPoseV2Model(model_config)
        # self.model = self.openpose_model.get_model()

        self.n_joints = len(hyper_config.kp_mapper) - 1
        self.n_limbs = len(hyper_config.connections)

        self.drawing_stick = hyper_config.drawing_stick
        self.drawer = Drawer(colors=self.hyper_config.colors,
                             n_limbs=self.n_limbs,
                             connections=self.hyper_config.connections,
                             stick=self.drawing_stick)

        self.use_gpu = hyper_config.use_gpu
        self.gpu_device_number = hyper_config.gpu_device_number
        self.pad_value = hyper_config.pad_value
        self.scales = hyper_config.scales

        self.do_pad_resizing = do_pad_resizing
        self.verbose = verbose

    def get_detections(self, img, multi_scale=False):

        """Returns a list of Detection objects, each one for a detected human.

        :param multi_scale: to use multi-scale inference.
        :param img: RGB(0, 255) image.
        """

        if self.do_pad_resizing:
            resized = self._resize_with_pad(img, self.input_res, self.pad_value)

        else:
            resized = cv2.resize(img, (self.input_res, self.input_res))

        t = time()
        if multi_scale:
            peaks, subset, candidate = self.openpose_model.multi_scale_inference(resized, 3)
        else:
            peaks, subset, candidate = self.openpose_model.estimate(resized)
        if self.verbose:
            print('_before_post_processing: ', time() - t)

        detections = list()
        if subset.any():
            org_h, org_w, _ = img.shape

            if self.do_pad_resizing:
                transformed_candidate = self._inverse_transform_candidate(org_h, org_w, candidate)
            else:
                transformed_candidate = self._inverse_transform_candidate_for_unpadded_resize(org_h, org_w, candidate)

            for i, person in enumerate(subset):
                kps, confidences = self._extract_keypoints(person, transformed_candidate)
                x_min, x_max, y_min, y_max = self._get_bbox(kps, org_w, org_h)
                bb = BoundingBox(x_min, x_max, y_min, y_max)
                p = Detection(kps,
                              transformed_candidate,
                              person,
                              confidences,
                              bb)
                detections.append(p)

        if self.verbose:
            print('inference time: ', time() - t)

        return detections

    def draw_detection(self,
                       image,
                       detection,
                       draw_bbox=True,
                       draw_id=True,
                       draw_kps=True,
                       draw_limbs=True):

        """Draw key-points, limbs, bounding boxes and ids on image.

            Use as follows:
                >> detections = openpose.get_detections(img)
                >> drawed = img.copy()
                >> for d in detections:
                >>     drawed = openpose.draw_detection()

            :arg image: input RGB(0, 255) image.
            :arg detection: Detection object.
            :arg draw_bbox: draw detection bounding box or not.
            :arg draw_id: draw detection id or not.
            :arg draw_kps: draw body joints or not.
            :arg draw_limbs: draw body limbs or not.
        """

        x_min, y_min, x_max, y_max = detection.bbox.data

        overlay = image.copy()

        # Draw bbox
        if draw_bbox:
            cv2.rectangle(overlay, (x_min, y_min), (x_max, y_max), detection.get_bbox_color(), 2, 5)

        # Draw id
        if draw_id:
            cv2.putText(overlay,
                        str(detection.id),
                        (x_min + (x_max - x_min) // 3, y_min - 10),
                        cv2.FONT_HERSHEY_SIMPLEX,
                        1,
                        detection.get_bbox_color(),
                        2)

        # Draw pose landmarks
        if draw_kps:
            self.drawer.draw_kps(overlay, detection.key_points)

        # Draw limbs
        if draw_limbs:
            overlay = self.drawer.draw_connections(overlay, detection.person_subset, detection.transformed_candidate)

        return overlay

    @staticmethod
    def _resize_with_pad(img, target_res, pad_value):
        org_res = np.array(img.shape[:2])
        ratio = float(target_res) / max(org_res)
        new_size = (org_res * ratio).astype(int)

        resized = cv2.resize(img, (new_size[1], new_size[0]))

        delta_w = target_res - new_size[1]
        delta_h = target_res - new_size[0]
        top, bottom = delta_h // 2, delta_h - (delta_h // 2)

        left, right = delta_w // 2, delta_w - (delta_w // 2)

        resized_padded = cv2.copyMakeBorder(resized, top, bottom, left, right, cv2.BORDER_CONSTANT,
                                            value=(pad_value, pad_value, pad_value))
        return resized_padded

    @staticmethod
    def _resize_aspect_ratio(img, scale_factor):
        org_h, org_w = np.array(img.shape[:2])
        new_w = (org_h * scale_factor).astype(int)
        new_h = (org_w * scale_factor).astype(int)

        resized = cv2.resize(img, (new_h, new_w))
        return resized

    def _get_bbox(self, kps, org_w, org_h):
        x_min, y_min, x_max, y_max = self._get_ul_lr(kps)
        diag = np.sqrt(np.square(x_min - x_max) + np.square(y_min - y_max))
        pad = diag // 10

        x_min = int(max(0, x_min - pad))
        y_min = int(max(0, y_min - pad))
        x_max = int(min(org_w, x_max + pad))
        y_max = int(min(org_h, y_max + pad))
        return x_min, x_max, y_min, y_max

    @staticmethod
    def _get_ul_lr(kps):
        # not_none_kps = np.array([kp for kp in kps if kp is not None])
        not_none_kps = kps[np.all(~np.isnan(kps), axis=1)]

        x_max_ind, y_max_ind = np.argmax(not_none_kps, axis=0)
        x_max = not_none_kps[x_max_ind, 0]
        y_max = not_none_kps[y_max_ind, 1]

        x_min_ind, y_min_ind = np.argmin(not_none_kps, axis=0)
        x_min = not_none_kps[x_min_ind, 0]
        y_min = not_none_kps[y_min_ind, 1]

        return x_min, y_min, x_max, y_max

    def _inverse_transform_candidate(self, org_h, org_w, candidate, preserve_aspect_ratio=False):
        if preserve_aspect_ratio:
            return self._inverse_tform_cand_preserve_ar(org_h, org_w, candidate)
        else:
            kps = candidate[:, 0: 2].astype(np.int)
            scale_factor = np.max([org_h, org_w]) / self.input_res
            transformed_candidate = np.zeros((candidate.shape[0], 3))
            if org_h > org_w:
                resized_w = org_w / scale_factor
                border = (self.input_res - resized_w) / 2
                for i, kp in enumerate(kps):
                    transformed_candidate[i, 0] = scale_factor * (kp[0] - border)
                    transformed_candidate[i, 1] = scale_factor * kp[1]
                    transformed_candidate[i, 2] = candidate[i, 2]
            else:
                resized_h = org_h / scale_factor
                border = (self.input_res - resized_h) / 2
                for i, kp in enumerate(kps):
                    transformed_candidate[i, 0] = scale_factor * kp[0]
                    transformed_candidate[i, 1] = scale_factor * (kp[1] - border)
                    transformed_candidate[i, 2] = candidate[i, 2]
            return transformed_candidate

    def _inverse_transform_candidate_for_unpadded_resize(self, org_h, org_w, candidate):
        kps = candidate[:, 0: 2].astype(np.int)

        scale_h = org_w / self.input_res
        scale_w = org_h / self.input_res

        transformed_candidate = np.zeros((candidate.shape[0], 3))
        for i, kp in enumerate(kps):
            transformed_candidate[i, 0] = scale_h * kp[0]
            transformed_candidate[i, 1] = scale_w * kp[1]
            transformed_candidate[i, 2] = candidate[i, 2]
        return transformed_candidate

    def _inverse_tform_cand_preserve_ar(self, org_h, org_w, candidate):
        kps = candidate[:, 0: 2].astype(np.int)

        scale_h = org_h / self.input_res
        scale_w = org_w / self.input_res

        transformed_candidate = np.zeros((candidate.shape[0], 3))
        for i, kp in enumerate(kps):
            transformed_candidate[i, 0] = scale_h * kp[0]
            transformed_candidate[i, 1] = scale_w * kp[1]
            transformed_candidate[i, 2] = candidate[i, 2]
        return transformed_candidate

    def _extract_keypoints(self, person_subset, candidate_arr):

        """Extracts pose keypoints from OpenPose outputs.

        Returns an array of shape(self.n_joints, 2) with hidden joints of np.nan values.
        """

        kps = np.zeros((self.n_joints, 2))
        kps[:] = np.nan
        joint_confidences = np.zeros(self.n_joints)
        for i in range(self.n_joints):
            kp_ind = person_subset[i].astype(np.int)
            if not kp_ind == -1:
                kps[i] = candidate_arr[kp_ind, 0: 2]
                joint_confidences[i] = candidate_arr[kp_ind, 2]
        return kps, joint_confidences


class InterMediateOpenPose:

    def __init__(self,
                 model_config,
                 hyper_config,
                 verbose):
        self.hyper_config = hyper_config
        self.model_config = model_config
        self.openpose_model = FastOpenPoseV2Model(model_config)
        self.input_res = model_config.input_res

        self.model = self.openpose_model.get_model()

        self.n_joints = len(hyper_config.kp_mapper) - 1
        self.n_limbs = len(hyper_config.connections)

        self.use_gpu = hyper_config.use_gpu
        self.gpu_device_number = hyper_config.gpu_device_number
        self.pad_value = hyper_config.pad_value
        self.scales = hyper_config.scales

        self.verbose = verbose

    def estimate(self, img):
        """Img must be of shape (self.openpose_model.input_h, self.openpose_model.input_w)."""

        paf, masked_heatmap = self.model.predict(np.expand_dims(img, axis=0))
        all_peaks = self._get_peaks(masked_heatmap)
        connection_all, special_k = self._get_connections(paf[0], all_peaks)
        subset, candidate = self._get_subset(all_peaks, special_k, connection_all)
        return all_peaks, subset, candidate

    def multi_scale_inference(self, img, gauss_sigma):
        """Multi-scale inference on given image, based on self.scales ."""

        h, w = img.shape[:2]

        n_hm = 26
        n_paf = 52

        heatmaps = np.zeros((len(self.scales), h, w, n_hm))
        pafs = np.zeros((len(self.scales), h, w, n_paf))

        for i, scale in enumerate(self.scales):
            scaled_img = cv2.resize(img, dsize=None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
            hm, paf = self.openpose_model.base_model.predict(np.expand_dims(scaled_img, axis=0))
            heatmaps[i] = cv2.resize(hm[0], dsize=None, fx=1 / scale, fy=1 / scale, interpolation=cv2.INTER_CUBIC)
            pafs[i] = cv2.resize(paf[0], dsize=None, fx=1 / scale, fy=1 / scale, interpolation=cv2.INTER_CUBIC)
        mean_hm = np.expand_dims(np.mean(heatmaps, axis=0), axis=0)
        mean_paf = np.expand_dims(np.mean(pafs, axis=0), axis=0)

        if gauss_sigma:
            mean_hm = self._tf_gauss_filter(mean_hm, gauss_sigma)

        mean_hm = self._get_masked_hm(mean_hm, h, w)

        all_peaks = self._get_peaks(mean_hm)
        connection_all, special_k = self._get_connections(mean_paf[0], all_peaks)
        subset, candidate = self._get_subset(all_peaks, special_k, connection_all)
        return all_peaks, subset, candidate

    @staticmethod
    def _get_gaussian_kernel(sigma=3):
        mean = 0
        size = sigma * 3
        d = tfb.distributions.Normal(mean, sigma)
        vals = d.prob(tf.range(start=-size, limit=size + 1, dtype=tf.float32))
        gauss_kernel = tf.einsum('i,j->ij', vals, vals)
        return gauss_kernel

    def _tf_gauss_filter(self, mean_hm, sigma):
        gaussian_kernel = self._get_gaussian_kernel(sigma)
        depth_wise_gaussian_kernel = tf.expand_dims(
            tf.transpose(tf.keras.backend.repeat(gaussian_kernel, 26), perm=(0, 2, 1)), axis=-1)
        hm = tf.nn.depthwise_conv2d(mean_hm.astype('float32'),
                                    depth_wise_gaussian_kernel,
                                    [1, 1, 1, 1],
                                    'SAME')
        return hm

    def _get_masked_hm(self, mean_hm, h, w):
        paddings = tf.constant([[0, 0], [1, 1], [1, 1], [0, 0]])
        padded = tf.pad(mean_hm, paddings)

        slice1 = tf.slice(padded, [0, 0, 1, 0], [-1, h, w, -1])
        slice2 = tf.slice(padded, [0, 2, 1, 0], [-1, h, w, -1])
        slice3 = tf.slice(padded, [0, 1, 0, 0], [-1, h, w, -1])
        slice4 = tf.slice(padded, [0, 1, 2, 0], [-1, h, w, -1])

        stacked = tf.stack([mean_hm >= slice1,
                            mean_hm >= slice2,
                            mean_hm >= slice3,
                            mean_hm >= slice4,
                            mean_hm >= self.model_config.joint_threshold], axis=-1)
        binary_hm = tf.reduce_all(stacked, axis=-1)

        masked_hm = tf.multiply(tf.cast(binary_hm, 'float'), mean_hm)
        return masked_hm

    def _get_peaks(self, masked_heatmap):
        t = time()
        ys, xs, channels = np.nonzero(masked_heatmap[0])

        all_peaks = list()
        peak_counter = 0
        for i in range(self.n_joints):
            indices = np.where(channels == i)[0]
            n_peaks = len(indices)
            peak_inds = range(peak_counter, peak_counter + n_peaks)
            part_peaks = list()
            for j, ind in enumerate(indices):
                x = xs[ind]
                y = ys[ind]
                conf_score = masked_heatmap[0, y, x, i]
                part_peaks.append((x, y, conf_score, peak_inds[j]))
            all_peaks.append(part_peaks)
            peak_counter = peak_counter + n_peaks
        if self.verbose:
            print('_get_peaks: ', time() - t)
        return all_peaks

    def _get_connections(self, paf, all_peaks):
        t = time()
        connection_all = []
        special_k = []
        mid_num = 10

        for k in range(len(self.hyper_config.map_paf_to_connections)):
            score_mid = paf[:, :, self.hyper_config.map_paf_to_connections[k]]
            cand_a = all_peaks[self.hyper_config.connections[k][0]]
            cand_b = all_peaks[self.hyper_config.connections[k][1]]
            n_a = len(cand_a)
            n_b = len(cand_b)
            if n_a != 0 and n_b != 0:
                connection_candidate = []
                for i in range(n_a):
                    for j in range(n_b):
                        vec = np.subtract(cand_b[j][:2], cand_a[i][:2])
                        norm = np.linalg.norm(vec)
                        # failure case when 2 body parts overlaps
                        if norm == 0:
                            continue
                        vec = np.divide(vec, norm)

                        points_y_pos = np.round(np.linspace(cand_a[i][0], cand_b[j][0], num=mid_num)).astype(np.int)
                        points_x_pos = np.round(np.linspace(cand_a[i][1], cand_b[j][1], num=mid_num)).astype(np.int)

                        vec_x = score_mid[points_x_pos, points_y_pos, 0]
                        vec_y = score_mid[points_x_pos, points_y_pos, 1]

                        score_mid_pts = np.multiply(vec_x, vec[0]) + np.multiply(vec_y, vec[1])
                        score_with_dist_prior = sum(score_mid_pts) / len(score_mid_pts) + min(
                            0.5 * self.input_res / norm - 1, 0)
                        criterion1 = len(np.nonzero(score_mid_pts > self.openpose_model.connection_threshold)[0]) > 0.8 * len(
                            score_mid_pts)
                        criterion2 = score_with_dist_prior > 0
                        if criterion1 and criterion2:
                            connection_candidate.append([i, j, score_with_dist_prior,
                                                         score_with_dist_prior + cand_a[i][2] + cand_b[j][2]])

                connection_candidate = sorted(connection_candidate, key=lambda x: x[2], reverse=True)
                connection = np.zeros((0, 5))
                for c in range(len(connection_candidate)):
                    i, j, s = connection_candidate[c][0:3]
                    if i not in connection[:, 3] and j not in connection[:, 4]:
                        connection = np.vstack([connection, [cand_a[i][3], cand_b[j][3], s, i, j]])
                        if len(connection) >= min(n_a, n_b):
                            break

                connection_all.append(connection)
            else:
                special_k.append(k)
                connection_all.append([])
        if self.verbose:
            print('_get_connections: ', time() - t)
        return connection_all, special_k

    def _get_subset(self,
                    all_peaks,
                    special_k,
                    connection_all):
        t = time()
        subset = -1 * np.ones((0, self.n_joints + 3))
        candidate = np.array([item for sublist in all_peaks for item in sublist])

        for k in range(len(self.hyper_config.map_paf_to_connections)):
            if k not in special_k:
                part_as = connection_all[k][:, 0]
                part_bs = connection_all[k][:, 1]
                index_a, index_b = np.array(self.hyper_config.connections[k])

                for i in range(len(connection_all[k])):  # = 1:size(temp,1)
                    found = 0
                    subset_idx = [-1, -1]
                    for j in range(len(subset)):  # 1:size(subset,1):
                        if subset[j][index_a] == part_as[i] or subset[j][index_b] == part_bs[i]:
                            subset_idx[found] = j
                            found += 1

                    if found == 1:
                        j = subset_idx[0]
                        if subset[j][index_b] != part_bs[i]:
                            subset[j][index_b] = part_bs[i]
                            subset[j][-1] += 1
                            subset[j][-2] += candidate[part_bs[i].astype(int), 2] + connection_all[k][i][2]
                    elif found == 2:  # if found 2 and disjoint, merge them
                        j1, j2 = subset_idx
                        membership = ((subset[j1] >= 0).astype(int) + (subset[j2] >= 0).astype(int))[:-2]
                        if len(np.nonzero(membership == 2)[0]) == 0:  # merge
                            subset[j1][:-2] += (subset[j2][:-2] + 1)
                            subset[j1][-2:] += subset[j2][-2:]
                            subset[j1][-2] += connection_all[k][i][2]
                            subset = np.delete(subset, j2, 0)
                        else:  # as like found == 1
                            subset[j1][index_b] = part_bs[i]
                            subset[j1][-1] += 1
                            subset[j1][-2] += candidate[part_bs[i].astype(int), 2] + connection_all[k][i][2]

                    # if find no partA in the subset, create a new subset
                    elif not found and k < self.n_limbs:
                        row = -1 * np.ones(self.n_joints + 3)
                        row[index_a] = part_as[i]
                        row[index_b] = part_bs[i]
                        row[-1] = 2
                        row[-2] = sum(candidate[connection_all[k][i, :2].astype(int), 2]) + connection_all[k][i][2]
                        subset = np.vstack([subset, row])

        # delete some rows of subset which has few parts occur
        delete_idx = []
        for i in range(len(subset)):
            if subset[i][-1] < self.model_config.min_vis_parts or subset[i][-2] / subset[i][-1] < 0.4:
                delete_idx.append(i)
        subset = np.delete(subset, delete_idx, axis=0)
        if self.verbose:
            print('_get_subset: ', time() - t)
        return subset, candidate


class FastOpenPoseV2Model:

    """Fast OpenPose body25 model.

    Key-point extraction implemented as graph definitions

    Note: pass the image in shape=input_res as RGB(0, 255)
    Note: Resize and pad the image to (input_res, input_res), this gives better results.
    """

    def __init__(self,
                 config):
        self.weights_path = config.weights_path

        # if input_h is None and input_w is None:
        #     self.input_h = config.input_res
        #     self.input_w = config.input_res
        #     self.input_res = config.input_res
        # else:
        #     self.input_w = input_w
        #     self.input_h = input_h
        #     if input_h == input_w:
        #         self.input_res = input_h
        #     else:
        #         self.input_res = None
        self.input_h = config.input_res
        self.input_w = config.input_res
        self.input_res = config.input_res

        self.use_gaussian_filtering = config.use_gaussian_filtering
        self.gaussian_kernel_sigma = config.gaussian_kernel_sigma
        self.resize_method = config.resize_method
        self.joint_threshold = config.joint_threshold
        self.connection_threshold = config.connection_threshold
        self.base_model = None

    def get_model(self):
        model = self._create_model()
        print('Model loaded successfully')
        return model

    def _create_model(self):
        openpose_model = OpenPoseModelV2(resize_method=self.resize_method)
        openpose_raw = openpose_model.create_model()
        openpose_raw.load_weights(self.weights_path)
        self.base_model = openpose_raw

        input_tensor = tfkl.Input(shape=(self.input_h, self.input_w, 3))
        hm, paf = openpose_raw(input_tensor)

        if self.use_gaussian_filtering:
            gaussian_kernel = self._get_gaussian_kernel(self.gaussian_kernel_sigma)
            depth_wise_gaussian_kernel = tf.expand_dims(
                tf.transpose(tf.keras.backend.repeat(gaussian_kernel, openpose_model.np_cm), perm=(0, 2, 1)), axis=-1)
            hm = tf.nn.depthwise_conv2d(hm,
                                        depth_wise_gaussian_kernel,
                                        [1, 1, 1, 1],
                                        'SAME')

        paddings = tf.constant([[0, 0], [1, 1], [1, 1], [0, 0]])
        padded = tf.pad(hm, paddings)

        slice1 = tf.slice(padded, [0, 0, 1, 0], [-1, self.input_h, self.input_w, -1])
        slice2 = tf.slice(padded, [0, 2, 1, 0], [-1, self.input_h, self.input_w, -1])
        slice3 = tf.slice(padded, [0, 1, 0, 0], [-1, self.input_h, self.input_w, -1])
        slice4 = tf.slice(padded, [0, 1, 2, 0], [-1, self.input_h, self.input_w, -1])

        stacked = tf.stack([hm >= slice1,
                            hm >= slice2,
                            hm >= slice3,
                            hm >= slice4,
                            hm >= self.joint_threshold], axis=-1)
        binary_hm = tf.reduce_all(stacked, axis=-1)

        masked_hm = tf.multiply(tf.cast(binary_hm, 'float'), hm)

        model = tfk.Model(input_tensor, [paf, masked_hm])
        return model

    @staticmethod
    def _get_gaussian_kernel(sigma=3):
        mean = 0
        size = sigma * 3
        d = tfb.distributions.Normal(mean, sigma)
        vals = d.prob(tf.range(start=-size, limit=size + 1, dtype=tf.float32))
        gauss_kernel = tf.einsum('i,j->ij', vals, vals)
        return gauss_kernel


class OpenPoseModelV2:

    def __init__(self,
                 input_shape=(None, None, 3),
                 paf_stages=4,
                 cm_stages=2,
                 np_paf=52,
                 np_cm=26,
                 resize_method='bicubic',
                 return_vgg=False):

        """OpenPose model definition proposed in arXiv:1812.08008v2

        :param input_shape: (height, width, n_channels)
        :param paf_stages: int - number of blocks for part affinity fields
        :param cm_stages: int - number of blocks for key-point heat-maps, i.e. confidence maps
        :param np_paf: int - number of channels for part affinity fields
        :param np_cm: int - number of channels for confidence maps, i.e. number of joints plus 1 for background

        Note: Input image must be RGB(0, 255)
        """

        self.input_shape = input_shape
        self.paf_stages = paf_stages
        self.cm_stages = cm_stages
        self.np_paf = np_paf
        self.np_cm = np_cm
        self.resize_method = resize_method
        self.return_vgg = return_vgg

    def create_model(self):
        input_tensor = tfkl.Input(self.input_shape)  # Input must be RGB and (0, 255)
        dynamic_input_res = tf.shape(input_tensor)[1:3]
        normalized_input = tfkl.Lambda(lambda x: x / 256 - 0.5)(input_tensor)  # [-0.5, 0.5]

        # VGG
        initial_features = self._vgg_block(normalized_input)

        # PAF blocks
        paf_out = self._paf_block(initial_features, 0, 96, 256)

        for paf_stage in range(1, self.paf_stages):
            concat = tfkl.Concatenate(axis=-1, name='concat_stage{}_L2'.format(paf_stage))([initial_features, paf_out])
            paf_out = self._paf_block(concat, paf_stage, 128, 512)

        # Confidence maps blocks
        cm_input = tfkl.Concatenate(axis=-1, name='concat_stage0_L1')([initial_features, paf_out])
        cm_out = self._cm_block(cm_input, 0, 96, 256)

        for cm_stage in range(1, self.cm_stages):
            concat = tfkl.Concatenate(axis=-1, name='concat_stage{}_L1'.format(cm_stage))(
                [initial_features, cm_out, paf_out])
            cm_out = self._cm_block(concat, cm_stage, 128, 512)

        cm_out = tf.image.resize(cm_out, dynamic_input_res, self.resize_method)
        paf_out = tf.image.resize(paf_out, dynamic_input_res, self.resize_method)
        if self.return_vgg:
            vgg_resized = tf.image.resize(initial_features, dynamic_input_res, self.resize_method)
            model = tfk.Model(input_tensor, [cm_out, paf_out, vgg_resized])
        else:
            model = tfk.Model(input_tensor, [cm_out, paf_out])
        return model

    def _paf_block(self, x, stage, n_kernels, n_pw_kernels):
        x = self._res_conv(x, stage, 1, n_kernels, 'L2')
        x = self._res_conv(x, stage, 2, n_kernels, 'L2')
        x = self._res_conv(x, stage, 3, n_kernels, 'L2')
        x = self._res_conv(x, stage, 4, n_kernels, 'L2')
        x = self._res_conv(x, stage, 5, n_kernels, 'L2')
        x = self._conv(x, n_pw_kernels, 1, 'Mconv6_stage{}_L2'.format(stage))
        x = self._prelu(x, 'Mprelu6_stage{}_L2'.format(stage))
        x = self._conv(x, self.np_paf, 1, 'Mconv7_stage{}_L2'.format(stage))
        return x

    def _cm_block(self, x, stage, n_kernels, n_pw_kernels):
        x = self._res_conv(x, stage, 1, n_kernels, 'L1')
        x = self._res_conv(x, stage, 2, n_kernels, 'L1')
        x = self._res_conv(x, stage, 3, n_kernels, 'L1')
        x = self._res_conv(x, stage, 4, n_kernels, 'L1')
        x = self._res_conv(x, stage, 5, n_kernels, 'L1')
        x = self._conv(x, n_pw_kernels, 1, 'Mconv6_stage{}_L1'.format(stage))
        x = self._prelu(x, 'Mprelu6_stage{}_L1'.format(stage))
        x = self._conv(x, self.np_cm, 1, 'Mconv7_stage{}_L1'.format(stage))
        return x

    def _res_conv(self, x, stage, block, n_kernels, block_type):
        conv_name = 'Mconv{}_stage{}_{}_'.format(block, stage, block_type)
        activation_name = 'Mprelu{}_stage{}_{}_'.format(block, stage, block_type)
        out1 = self._conv(x, n_kernels, 3, conv_name + str(0))
        out1 = self._prelu(out1, activation_name + str(0))

        out2 = self._conv(out1, n_kernels, 3, conv_name + str(1))
        out2 = self._prelu(out2, activation_name + str(1))

        out3 = self._conv(out2, n_kernels, 3, conv_name + str(2))
        out3 = self._prelu(out3, activation_name + str(2))

        out = tfkl.Concatenate(axis=-1, name=conv_name + 'concat')([out1, out2, out3])
        return out

    def _vgg_block(self, x):
        # Block 1
        x = self._conv(x, 64, 3, "conv1_1")
        x = self._relu(x, 'relu1_1')
        x = self._conv(x, 64, 3, "conv1_2")
        x = self._relu(x, 'relu1_2')
        x = self._pooling(x, 2, 2, "pool1_stage1")

        # Block 2
        x = self._conv(x, 128, 3, "conv2_1")
        x = self._relu(x, 'relu2_1')
        x = self._conv(x, 128, 3, "conv2_2")
        x = self._relu(x, 'relu2_2')
        x = self._pooling(x, 2, 2, "pool2_stage1")

        # Block 3
        x = self._conv(x, 256, 3, "conv3_1")
        x = self._relu(x, 'relu3_1')
        x = self._conv(x, 256, 3, "conv3_2")
        x = self._relu(x, 'relu3_2')
        x = self._conv(x, 256, 3, "conv3_3")
        x = self._relu(x, 'relu3_3')
        x = self._conv(x, 256, 3, "conv3_4")
        x = self._relu(x, 'relu3_4')
        x = self._pooling(x, 2, 2, "pool3_stage1")

        # Block 4
        x = self._conv(x, 512, 3, "conv4_1")
        x = self._relu(x, 'relu4_1')
        x = self._conv(x, 512, 3, "conv4_2")
        x = self._prelu(x, 'prelu4_2')

        # Additional non vgg layers
        x = self._conv(x, 256, 3, "conv4_3_CPM")
        x = self._prelu(x, 'prelu4_3_CPM')
        x = self._conv(x, 128, 3, "conv4_4_CPM")
        x = self._prelu(x, 'prelu4_4_CPM')
        return x

    @staticmethod
    def _conv(x, nf, ks, name):
        out = tfkl.Conv2D(nf, (ks, ks), padding='same', name=name)(x)
        return out

    @staticmethod
    def _relu(x, name):
        return tfkl.Activation('relu', name=name)(x)

    @staticmethod
    def _prelu(x, name):
        return tfkl.PReLU(shared_axes=[1, 2], name=name)(x)

    @staticmethod
    def _pooling(x, ks, st, name):
        x = tfkl.MaxPooling2D((ks, ks), strides=(st, st), name=name)(x)
        return x
