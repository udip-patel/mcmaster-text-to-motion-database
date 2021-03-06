import math
import os
import numpy as np
import tensorflow as tf
from pose_utils.sparse_to_dense import sparse_joints_to_dense_single_example
from dataset.mpii_datatypes import Person

EXAMPLES_PER_SHARD = 256
LEFT_RIGHT_FLIPPED_INDICES = [5, 4, 3, 2, 1, 0, 6, 7, 8, 9, 15, 14, 13, 12, 11, 10]

class EvalBatch(object):
    """Contains an evaluation batch of images along with corresponding
    ground-truth joint vectors for the annotated person in that image.

    images, *_joints, joint_indices and head_size should all be lists of length
    `batch_size`.
    """
    def __init__(self,
                 images,
                 binary_maps,
                 heatmaps,
                 weights,
                 is_visible_weights,
                 joint_indices,
                 x_joints,
                 y_joints,
                 head_size,
                 batch_size):
        assert images.get_shape()[0] == batch_size
        self._images = images
        self._binary_maps = binary_maps
        self._heatmaps = heatmaps
        self._weights = weights
        self._is_visible_weights = is_visible_weights
        self._joint_indices = joint_indices
        self._x_joints = x_joints
        self._y_joints = y_joints
        self._head_size = head_size
        self._batch_size = batch_size

    @property
    def images(self):
        return self._images

    @property
    def binary_maps(self):
        return self._binary_maps

    @property
    def heatmaps(self):
        return self._heatmaps

    @property
    def weights(self):
        return self._weights

    @property
    def is_visible_weights(self):
        return self._is_visible_weights

    @property
    def joint_indices(self):
        return self._joint_indices

    @property
    def x_joints(self):
        return self._x_joints

    @property
    def y_joints(self):
        return self._y_joints

    @property
    def head_size(self):
        return self._head_size

    @property
    def batch_size(self):
        return self._batch_size


def _setup_example_queue(filename_queue,
                         num_readers,
                         input_queue_memory_factor,
                         batch_size):
    """Sets up a randomly shuffled queue containing example protobufs, read
    from the TFRecord files in `filename_queue`.

    Args:
        filename_queue: A queue of filepaths to the TFRecord files containing
            the input Example protobufs.
        num_readers: Number of file readers to use.
        input_queue_memory_factor: Factor by which to scale up the minimum
            number of examples in the example queue. A larger factor increases
            the mixing of examples, but will also increase memory pressure.
        batch_size: Number of training elements in a batch.

    Returns:
        A dequeue op that will dequeue one Tensor containing an input example
        from `examples_queue`.
    """
    min_queue_examples = input_queue_memory_factor*EXAMPLES_PER_SHARD

    examples_queue = tf.RandomShuffleQueue(capacity=min_queue_examples + 3*batch_size,
                                           min_after_dequeue=min_queue_examples,
                                           dtypes=[tf.string])

    enqueue_ops = []
    for _ in range(num_readers):
        options = tf.python_io.TFRecordOptions(
            compression_type=tf.python_io.TFRecordCompressionType.ZLIB)
        reader = tf.TFRecordReader(options=options)
        _, per_thread_example = reader.read(queue=filename_queue)
        enqueue_ops.append(examples_queue.enqueue(vals=[per_thread_example]))

    tf.train.queue_runner.add_queue_runner(
        qr=tf.train.queue_runner.QueueRunner(queue=examples_queue,
                                             enqueue_ops=enqueue_ops))

    return examples_queue.dequeue()


def _parse_example_proto(example_serialized, image_dim):
    """Parses an example proto and returns a tuple containing
    (raw image reshaped to the image dimensions in float32 format, sparse joint
    indices, sparse joints).
    """
    feature_map = {
        'image_jpeg': tf.FixedLenFeature(shape=[], dtype=tf.string),
        'binary_maps': tf.FixedLenFeature(shape=[], dtype=tf.string),
        'joint_indices': tf.VarLenFeature(dtype=tf.int64),
        'is_visible_list': tf.VarLenFeature(dtype=tf.int64),
        'x_joints': tf.VarLenFeature(dtype=tf.float32),
        'y_joints': tf.VarLenFeature(dtype=tf.float32),
        'head_size': tf.FixedLenFeature(shape=[], dtype=tf.float32)
    }

    features = tf.parse_single_example(
        serialized=example_serialized, features=feature_map)

    img_jpeg = features['image_jpeg']
    with tf.name_scope(name='decode_jpeg', values=[img_jpeg]):
        img_tensor = tf.image.decode_jpeg(contents=img_jpeg,
                                          channels=3)
        decoded_img = tf.image.convert_image_dtype(
            image=img_tensor, dtype=tf.float32)

    parsed_example = {'image': decoded_img,
                      'binary_maps': features['binary_maps'],
                      'joint_indices': features['joint_indices'],
                      'x_joints': features['x_joints'],
                      'y_joints': features['y_joints'],
                      'head_size': features['head_size'],
                      'is_visible_list': features['is_visible_list']}

    return parsed_example


def _distort_colour(distorted_image, thread_id):
    """Distorts the brightness, saturation, hue and contrast of an image
    randomly, and returns the result.

    The colour distortions are non-commutative, so we do them in a random order
    per thread (based on `thread_id`).
    """
    colour_ordering = thread_id % 2

    distorted_image = tf.image.random_brightness(image=distorted_image, max_delta=32./255.)
    if colour_ordering == 0:
        distorted_image = tf.image.random_saturation(image=distorted_image, lower=0.5, upper=1.5)
        distorted_image = tf.image.random_hue(image=distorted_image, max_delta=0.2)
        distorted_image = tf.image.random_contrast(image=distorted_image, lower=0.5, upper=1.5)
    else:
        distorted_image = tf.image.random_contrast(image=distorted_image, lower=0.5, upper=1.5)
        distorted_image = tf.image.random_saturation(image=distorted_image, lower=0.5, upper=1.5)
        distorted_image = tf.image.random_hue(image=distorted_image, max_delta=0.2)

    return tf.clip_by_value(t=distorted_image, clip_value_min=0.0, clip_value_max=1.0)


def _decode_binary_maps(binary_maps, image_dim):
    """Decodes a binary map for an example from its post-decompression format
    (uint8), and reshapes the tensor so that TensorFlow will understand it.
    """
    binary_maps = tf.decode_raw(bytes=binary_maps, out_type=tf.uint8)
    binary_maps = tf.reshape(tensor=binary_maps,
                             shape=[image_dim, image_dim, Person.NUM_JOINTS])

    return tf.cast(binary_maps, tf.float32)


def _flip_with_left_right_permutation(maps):
    """Flips ground truth maps, accounting for the fact that when mirrored the
    images' left and right have also been swapped.

    TODO(brendan): Apparently `tf.transpose` is slow.
    Replace with `tf.reshape` -> replace gather column indices with a list of
    individual element indices?
    """
    maps = tf.image.flip_left_right(image=maps)

    maps = tf.transpose(a=maps, perm=[2, 0, 1])
    maps = tf.gather(params=maps,
                     indices=LEFT_RIGHT_FLIPPED_INDICES)

    return tf.transpose(a=maps, perm=[1, 2, 0])


def _maybe_flip_maps(maps, should_flip):
    """Conditionally flip ground truth maps left-right (i.e. left becomes right
    and vice versa).
    """
    return tf.cond(pred=should_flip,
                   fn1=lambda: _flip_with_left_right_permutation(maps),
                   fn2=lambda: maps)


def _randomly_flip(image, binary_maps, heatmaps):
    """Randomly flips an image and set of joint-maps left or right, and returns
    the (possibly) flipped results.
    """
    rand_uniform = tf.random_uniform(shape=[],
                                     minval=0,
                                     maxval=1.0)
    should_flip = rand_uniform < 0.5
    flipped_image = tf.cond(
        pred=should_flip,
        fn1=lambda: tf.image.flip_left_right(image=image),
        fn2=lambda: image)

    flipped_binary_maps = _maybe_flip_maps(binary_maps, should_flip)
    flipped_heatmaps = _maybe_flip_maps(heatmaps, should_flip)

    return flipped_image, flipped_binary_maps, flipped_heatmaps, should_flip


def _randomly_rotate(image, binary_maps, heatmaps, max_rotation_angle):
    """Randomly rotates inputs between +/-`max_rotation_angle`, and returns the
    results.
    """
    rand_angle = tf.random_uniform(shape=[],
                                   minval=-max_rotation_angle,
                                   maxval=max_rotation_angle)

    rotated_image = tf.contrib.image.rotate(images=image, angles=rand_angle)

    rotated_maps = tf.contrib.image.rotate(images=[binary_maps, heatmaps],
                                           angles=rand_angle)
    rotated_binary_maps = rotated_maps[0]
    rotated_heatmaps = rotated_maps[1]

    return rotated_image, rotated_binary_maps, rotated_heatmaps


def _distort_image(decoded_image,
                   binary_maps,
                   heatmaps,
                   image_dim,
                   thread_id,
                   max_rotation_angle):
    """Randomly distorts the image from `parsed_example` by randomly rotating,
    randomly flipping left and right, and randomly distorting the colour of
    that image.

    Args:
        decoded_image: Raw image, decoded from JPEG.
        binary_maps: Binary maps of joint positions.
        heatmaps: Confidence maps of joint positions.
        image_dim: Dimension of the image as required when input to the
            network.
        thread_id: Number of the image preprocessing thread responsible for
            these image distortions.
        max_rotation_angle: Maximum amount to rotate images, in radians.

    Returns:
        (distorted_image, distorted_binary_maps, distorted_heatmaps) tuple
        containing joint-maps and image post flipping, rotation, and colour
        distortion.
    """
    decoded_image = tf.reshape(tensor=decoded_image,
                                 shape=[image_dim, image_dim, 3])
    binary_maps = _decode_binary_maps(binary_maps, image_dim)

    flipped_image, flipped_binary_maps, flipped_heatmaps, should_flip = _randomly_flip(
        decoded_image, binary_maps, heatmaps)

    distorted_image = _distort_colour(flipped_image, thread_id)

    distorted_image, distorted_binary_maps, distorted_heatmaps = _randomly_rotate(
        distorted_image, flipped_binary_maps, flipped_heatmaps, max_rotation_angle)

    distorted_image = tf.subtract(x=distorted_image, y=0.5)
    distorted_image = tf.multiply(x=distorted_image, y=2.0)

    return distorted_image, distorted_binary_maps, distorted_heatmaps, should_flip


def _parse_and_preprocess_example_eval(heatmap_stddev_pixels,
                                       example_serialized,
                                       num_preprocess_threads,
                                       image_dim):
    """Same as `_parse_and_preprocess_example_train`, except without image
    distortion or heatmap creation.

    Hence this function returns dense x and y joints, instead of dense
    heatmaps.
    """
    images_and_jointmaps = []
    for thread_id in range(num_preprocess_threads):
        parsed_example = _parse_example_proto(example_serialized, image_dim)

        decoded_img = parsed_example['image']
        binary_maps = parsed_example['binary_maps']
        joint_indices = parsed_example['joint_indices']
        x_joints = parsed_example['x_joints']
        y_joints = parsed_example['y_joints']

        binary_maps = _decode_binary_maps(binary_maps, image_dim)

        decoded_img = tf.reshape(tensor=decoded_img,
                                 shape=[image_dim, image_dim, 3])

        decoded_img = tf.subtract(x=decoded_img, y=0.5)
        decoded_img = tf.multiply(x=decoded_img, y=2.0)

        x_dense_joints, y_dense_joints, weights, sparse_joint_indices = sparse_joints_to_dense_single_example(
            x_joints, y_joints, joint_indices, Person.NUM_JOINTS)

        is_visible_weights = _get_is_visible_weights(sparse_joint_indices,
                                                     parsed_example['is_visible_list'].values,
                                                     weights)

        heatmaps = _get_joint_heatmaps(heatmap_stddev_pixels,
                                       image_dim,
                                       x_dense_joints,
                                       y_dense_joints)

        images_and_jointmaps.append([decoded_img,
                                     binary_maps,
                                     heatmaps,
                                     weights,
                                     is_visible_weights,
                                     joint_indices,
                                     x_joints,
                                     y_joints,
                                     parsed_example['head_size']])

    return images_and_jointmaps


def _get_joints_normal_pdf(dense_joints, std_dev, coords, expand_axis):
    """Creates a set of 1-D Normal distributions with means equal to the
    elements of `dense_joints`, and the standard deviations equal to the values
    in `std_dev`.

    The shapes of the input tensors `dense_joints` and `std_dev` must be the
    same.

    Args:
        dense_joints: Dense tensor of ground truth joint locations in 1-D.
        std_dev: Standard deviation to use for the normal distribution.
        coords: Set of co-ordinates over which to evaluate the Normal's PDF.
        expand_axis: Axis to expand the probabilities by using
            `tf.expand_dims`. E.g., an `expand_axis` of -2 will turn the result
            PDF output from a tensor with shape [16, 380] to a tensor with
            shape [16, 380, 1]


        3-D tensor with first dim being the length of `dense_joints`, and two
        more dimensions, one of which is the length of `coords` and the other
        of which has size 1.
    """
    normal = tf.contrib.distributions.Normal(dense_joints, std_dev)
    probs = normal.prob(coords)
    probs = tf.transpose(probs)

    return tf.expand_dims(input=probs, axis=expand_axis)


def _get_joint_heatmaps(heatmap_stddev_pixels,
                        image_dim,
                        x_dense_joints,
                        y_dense_joints):
    """Calculates a set of confidence maps for the joints given by
    `x_dense_joints` and `y_dense_joints`.

    The confidence maps are 2-D Gaussians with means given by the joints'
    (x, y) locations, and standard deviations given by `heatmap_stddev_pixels`.
    So, e.g. for a set of 16 joints and an image dimension of 380, a set of
    tensors with shape [380, 380, 16] will be returned, where each
    [380, 380, i] tensor will be a gaussian corresponding to the i'th joint.
    """
    std_dev = np.full(Person.NUM_JOINTS, heatmap_stddev_pixels/image_dim)
    std_dev = tf.cast(std_dev, tf.float32)

    pixel_spacing = np.linspace(-0.5, 0.5, image_dim)
    coords = np.empty((image_dim, Person.NUM_JOINTS), dtype=np.float32)
    coords[...] = pixel_spacing[:, None]

    x_probs = _get_joints_normal_pdf(x_dense_joints, std_dev, coords, -2)
    y_probs = _get_joints_normal_pdf(y_dense_joints, std_dev, coords, -1)

    heatmaps = tf.matmul(a=y_probs, b=x_probs)
    heatmaps = tf.transpose(a=heatmaps, perm=[1, 2, 0])

    return heatmaps

def _get_is_visible_weights(sparse_joint_indices, is_visible_list, weights):
    """Calculates and returns a set of per-joint weights, which are 1 if and
    only if the joint annotation is both present and unoccluded.
    """
    is_visible_dense = tf.sparse_to_dense(sparse_indices=sparse_joint_indices,
                                          output_shape=[Person.NUM_JOINTS],
                                          sparse_values=is_visible_list)
    return tf.multiply(weights, tf.cast(is_visible_dense, tf.float32))


def _maybe_flip_weights(weights, should_flip):
    """Conditionally permutes weights left-right (i.e. left becomes right)."""
    return tf.cond(pred=should_flip,
                   fn1=lambda: tf.gather(params=weights, indices=LEFT_RIGHT_FLIPPED_INDICES),
                   fn2=lambda: weights)


def _parse_and_preprocess_example_train(example_serialized,
                                        num_preprocess_threads,
                                        image_dim,
                                        heatmap_stddev_pixels,
                                        max_rotation_angle):
    """Parses Example protobufs containing input images and their ground truth
    vectors and preprocesses those images, returning a vector with one
    preprocessed tensor per thread.

    The loop over the threads (as opposed to a deep copy of the first resultant
    Tensor) allows for different image distortion to be done depending on the
    thread ID, although currently all image preprocessing is identical.

    Args:
        example_serialized: Tensor containing a serialized example, as read
            from a TFRecord file.
        num_preprocess_threads: Number of threads to use for image
            preprocessing.
        image_dim: Dimension of square input images.
        heatmap_stddev_pixels: Standard deviation of Gaussian joint heatmap, in
            pixels.
        max_rotation_angle: Maximum amount to rotate images, in radians.

    Returns:
        A list of lists, one for each thread, where each inner list contains a
        decoded image with colours scaled to range [-1, 1], as well as the
        sparse joint ground truth vectors.
    """
    images_and_joint_maps = []
    for thread_id in range(num_preprocess_threads):
        parsed_example = _parse_example_proto(example_serialized, image_dim)

        x_dense_joints, y_dense_joints, weights, sparse_joint_indices = sparse_joints_to_dense_single_example(
            parsed_example['x_joints'],
            parsed_example['y_joints'],
            parsed_example['joint_indices'],
            Person.NUM_JOINTS)

        is_visible_weights = _get_is_visible_weights(sparse_joint_indices,
                                                     parsed_example['is_visible_list'].values,
                                                     weights)

        heatmaps = _get_joint_heatmaps(heatmap_stddev_pixels,
                                       image_dim,
                                       x_dense_joints,
                                       y_dense_joints)

        distorted_image, binary_maps, heatmaps, should_flip = _distort_image(
            parsed_example['image'],
            parsed_example['binary_maps'],
            heatmaps,
            image_dim,
            thread_id,
            max_rotation_angle)

        weights = _maybe_flip_weights(weights, should_flip)
        is_visible_weights = _maybe_flip_weights(is_visible_weights, should_flip)

        images_and_joint_maps.append([distorted_image,
                                      binary_maps,
                                      heatmaps,
                                      weights,
                                      is_visible_weights])

    return images_and_joint_maps


def _setup_batch_queue(images_and_joint_maps, batch_size, num_preprocess_threads):
    """Sets up a batch queue that returns, e.g., a batch of 32 each of images,
    sparse joints and sparse joint indices.
    """
    images, binary_maps, heatmaps, weights, is_visible_weights = tf.train.batch_join(
        tensors_list=images_and_joint_maps,
        batch_size=batch_size,
        capacity=num_preprocess_threads*batch_size)

    image_dim = heatmaps.get_shape().as_list()[1]

    merged_heatmaps = tf.reshape(tf.reduce_max(heatmaps,3),[batch_size,image_dim, image_dim, 1])
    merged_heatmaps = tf.cast(merged_heatmaps, tf.float32)

    merged_binary_maps = tf.reshape(tf.reduce_max(binary_maps,3),[batch_size,image_dim, image_dim, 1])
    merged_binary_maps = tf.cast(merged_binary_maps, tf.float32)

    tf.summary.image(name='images', tensor=images)
    tf.summary.image(name='heatmaps', tensor=merged_heatmaps)
    tf.summary.image(name='binary_maps', tensor=merged_binary_maps)

    return images, binary_maps, heatmaps, weights, is_visible_weights


def setup_eval_input_pipeline(batch_size,
                              num_preprocess_threads,
                              image_dim,
                              heatmap_stddev_pixels,
                              data_filenames):
    """Sets up an input pipeline for model evaluation.

    This function is similar to `setup_train_input_pipeline`, except that
    images are not distorted, and the filename queue will process only one
    TFRecord at a time. Therefore no Example queue is needed.
    """
    filename_queue = tf.train.string_input_producer(
        string_tensor=data_filenames,
        shuffle=False,
        capacity=16)

    options = tf.python_io.TFRecordOptions(
        compression_type=tf.python_io.TFRecordCompressionType.ZLIB)
    reader = tf.TFRecordReader(options=options)
    _, example_serialized = reader.read(filename_queue)

    images_and_joint_maps = _parse_and_preprocess_example_eval(
        heatmap_stddev_pixels,
        example_serialized,
        num_preprocess_threads,
        image_dim)

    images, binary_maps, heatmaps, weights, is_visible_weights, joint_indices, x_joints, y_joints, head_size = tf.train.batch_join(
        tensors_list=images_and_joint_maps,
        batch_size=batch_size,
        capacity=num_preprocess_threads*batch_size)

    return EvalBatch(images,
                     binary_maps,
                     heatmaps,
                     weights,
                     is_visible_weights,
                     joint_indices,
                     x_joints,
                     y_joints,
                     head_size,
                     batch_size)


def setup_train_input_pipeline(FLAGS, data_filenames):
    """Sets up an input pipeline that reads example protobufs from all TFRecord
    files, assumed to be named train*.tfrecord (e.g. train0.tfrecord),
    decodes and preprocesses the images.

    There are three queues: `filename_queue`, contains the TFRecord filenames,
    and feeds `examples_queue` with serialized example protobufs.

    Then, serialized examples are dequeued from `examples_queue`, preprocessed
    in parallel by `num_preprocess_threads` and the result is enqueued into the
    queue created by `batch_join`. A dequeue operation from the `batch_join`
    queue is what is returned from `preprocess_images`. What is dequeued is a
    batch of size `batch_size` containing a set of, for example, 32 images in
    the case of `images` or a sparse vector of floating-point joint
    co-ordinates (in range [0,1]) in the case of `joints`.

    Also adds a summary for the images.

    Args:
        num_readers: Number of file readers to use.
        input_queue_memory_factor: Input to `_setup_example_queue`. See
            `_setup_example_queue` for details.
        batch_size: Number of examples to process at once (in one training
            step).
        num_preprocess_threads: Number of threads to use to preprocess image
            data.
        image_dim: Dimension of square input images.
        data_filenames: Set of filenames to get examples from.
        max_rotation_angle: Maximum amount to rotate images, in radians.

    Returns:
        (images, heatmaps, weights, batch_size): List of image
        tensors with first dimension (shape[0]) equal to batch_size, along with
        lists of dense vectors of heatmaps of ground truth vectors (joints),
        dense vectors of weights and the batch size.
    """

    num_readers = FLAGS.num_readers
    input_queue_memory_factor = FLAGS.input_queue_memory_factor
    batch_size = FLAGS.batch_size
    num_preprocess_threads = FLAGS.num_preprocess_threads
    image_dim = FLAGS.image_dim
    heatmap_stddev_pixels = FLAGS.heatmap_stddev_pixels
    max_rotation_angle = FLAGS.max_rotation_angle

    # TODO(brendan): num_readers == 1 case
    assert num_readers > 1

    with tf.name_scope('batch_processing'):
        filename_queue = tf.train.string_input_producer(
            string_tensor=data_filenames,
            shuffle=True,
            capacity=16)

        example_serialized = _setup_example_queue(filename_queue,
                                                  num_readers,
                                                  input_queue_memory_factor,
                                                  batch_size)

        images_and_joint_maps = _parse_and_preprocess_example_train(
            example_serialized,
            num_preprocess_threads,
            image_dim,
            heatmap_stddev_pixels,
            max_rotation_angle)

        return _setup_batch_queue(images_and_joint_maps,
                                  batch_size,
                                  num_preprocess_threads)
