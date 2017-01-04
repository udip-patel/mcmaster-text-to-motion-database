import tensorflow as tf
import tensorflow.contrib.slim as slim
from tensorflow.python.platform import tf_logging
from logging import INFO
from tensorflow.contrib.slim.nets import inception
from input_pipeline import setup_train_input_pipeline

FLAGS = tf.app.flags.FLAGS

NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN = 18076

RMSPROP_DECAY = 0.9
RMSPROP_MOMENTUM = 0.9
RMSPROP_EPSILON = 1.0

NUM_JOINTS = 16
NUM_JOINT_COORDS = 2*NUM_JOINTS

tf.app.flags.DEFINE_string('data_dir', '.',
                           """Path to take input TFRecord files from.""")
tf.app.flags.DEFINE_string('log_dir', './log',
                           """Path to take summaries and checkpoints from, and
                           write them to.""")

tf.app.flags.DEFINE_integer('image_dim', 299,
                            """Dimension of the square input image.""")

tf.app.flags.DEFINE_integer('num_preprocess_threads', 4,
                            """Number of threads to use to preprocess
                            images.""")
tf.app.flags.DEFINE_integer('num_readers', 4,
                            """Number of threads to use to read example
                            protobufs from TFRecords.""")
tf.app.flags.DEFINE_integer('num_gpus', 1,
                            """Number of GPUs in system.""")

tf.app.flags.DEFINE_integer('batch_size', 32,
                            """Size of each mini-batch (number of examples
                            processed at once).""")
tf.app.flags.DEFINE_integer('input_queue_memory_factor', 16,
                            """Factor by which to increase the minimum examples
                            in RandomShuffleQueue.""")

tf.app.flags.DEFINE_float('initial_learning_rate', 0.1,
                          """Initial learning rate.""")
tf.app.flags.DEFINE_float('learning_rate_decay_factor', 0.16,
                          """Rate at which learning rate is decayed.""")
tf.app.flags.DEFINE_float('num_epochs_per_decay', 30.0,
                          """Number of epochs before decay factor is applied
                          once.""")

def _summarize_inception_model(endpoints):
    """Summarizes the activation values that are marked for summaries in the
    Inception v3 network.
    """
    with tf.name_scope('summaries'):
        for activation in endpoints.values():
            tensor_name = activation.op.name
            tf.summary.histogram(
                name=tensor_name + '/activations',
                values=activation)
            tf.summary.scalar(
                name=tensor_name + '/sparsity',
                tensor=tf.nn.zero_fraction(value=activation))


def _sparse_joints_to_dense(training_batch, num_joint_coords):
    """Converts a sparse vector of joints to a dense format, and also returns a
    set of weights indicating which joints are present.

    Args:
        training_batch: A batch of training images with associated joint
            vectors.
        num_joint_coords: Total number of joint co-ordinates, where (x0, y0)
            counts as two co-ordinates.

    Returns:
        (dense_joints, weights) tuple, where dense_joints is a dense vector of
        shape [batch_size, num_joint_coords], with zeros in the indices not
        present in the sparse vector. `weights` contains 1s for all the present
        joints and 0s otherwise.
    """
    sparse_joints = tf.sparse_merge(sp_ids=training_batch.joint_indices,
                                    sp_values=training_batch.joints,
                                    vocab_size=num_joint_coords)
    dense_joints = tf.sparse_tensor_to_dense(sp_input=sparse_joints,
                                             default_value=0)

    dense_shape = [training_batch.batch_size, num_joint_coords]
    dense_joints = tf.reshape(tensor=dense_joints, shape=dense_shape)

    weights = tf.sparse_to_dense(sparse_indices=sparse_joints.indices,
                                 output_shape=dense_shape,
                                 sparse_values=1,
                                 default_value=0)

    return dense_joints, weights


def _inference(training_batch, num_joint_coords, scope):
    """Sets up an Inception v3 model, computes predictions on input images and
    calculates loss on those predictions based on an input sparse vector of
    joints (the ground truth vector).

    TF-slim's `arg_scope` is used to keep variables (`slim.model_variable`) in
    CPU memory. See the training procedure block diagram in the TF Inception
    [README](https://github.com/tensorflow/models/tree/master/inception).

    Args:
        training_batch: A batch of training images with associated joint
            vectors.
        num_joint_coords: Total number of joint co-ordinates, where (x0, y0)
            counts as two co-ordinates.
        scope: The name scope (for summaries, debugging).

    Returns:
        Tensor giving the total loss (combined loss from auxiliary and primary
        logits, added to regularization losses).
    """
    with slim.arg_scope([slim.model_variable], device='/cpu:0'):
        with slim.arg_scope(inception.inception_v3_arg_scope()):
            logits, endpoints = inception.inception_v3(inputs=training_batch.images,
                                                       num_classes=num_joint_coords,
                                                       scope=scope)

            _summarize_inception_model(endpoints)

            dense_joints, weights = _sparse_joints_to_dense(training_batch,
                                                            num_joint_coords)

            auxiliary_logits = endpoints['AuxLogits']

            slim.losses.mean_squared_error(predictions=logits,
                                           labels=dense_joints,
                                           weights=weights)
            slim.losses.mean_squared_error(predictions=auxiliary_logits,
                                           labels=dense_joints,
                                           weights=weights,
                                           scope='aux_logits')

            # TODO(brendan): Calculate loss averages for tensorboard

            total_loss = slim.losses.get_total_loss()

    return total_loss


def _setup_optimizer(batch_size,
                     num_epochs_per_decay,
                     initial_learning_rate,
                     learning_rate_decay_factor):
    """Sets up the optimizer
    [RMSProp]((http://www.cs.toronto.edu/~tijmen/csc321/slides/lecture_slides_lec6.pdf))
    and learning rate decay schedule.

    Args:
        batch_size: Number of elements in training batch.
        num_epochs_per_decay: Number of full runs through of the data set per
            learning rate decay.
        initial_learning_rate: Learning rate to start with on step 0.
        learning_rate_decay_factor: Factor by which to decay the learning rate.

    Returns:
        global_step: Counter to be incremented each training step, used to
            calculate the decaying learning rate.
        optimizer: `RMSPropOptimizer`, minimizes loss function by gradient
            descent (RMSProp).
    """
    global_step = tf.get_variable(
        name='global_step',
        shape=[],
        dtype=tf.int64,
        initializer=tf.constant_initializer(0),
        trainable=False)

    num_batches_per_epoch = (NUM_EXAMPLES_PER_EPOCH_FOR_TRAIN / batch_size)
    decay_steps = int(num_batches_per_epoch*num_epochs_per_decay)
    learning_rate = tf.train.exponential_decay(learning_rate=initial_learning_rate,
                                               global_step=global_step,
                                               decay_steps=decay_steps,
                                               decay_rate=learning_rate_decay_factor)

    optimizer = tf.train.RMSPropOptimizer(learning_rate=learning_rate,
                                          decay=RMSPROP_DECAY,
                                          momentum=RMSPROP_MOMENTUM,
                                          epsilon=RMSPROP_EPSILON)

    return global_step, optimizer


def _setup_training_op(training_batch,
                       num_joint_coords,
                       global_step,
                       optimizer):
    """Sets up inference (predictions), loss calculation, and minimization
    based on the input optimizer.

    Args:
        training_batch: Batch of preprocessed examples dequeued from the input
            pipeline.
        global_step: Training step counter.
        optimizer: Optimizer to minimize the loss function.

    Returns: Operation to run a training step.
    """
    with tf.device(device_name_or_function='/gpu:0'):
        with tf.name_scope(name='tower0') as scope:
            loss = _inference(training_batch, num_joint_coords, scope)

            train_op = slim.learning.create_train_op(
                total_loss=loss,
                optimizer=optimizer,
                global_step=global_step)

    return train_op


def train():
    """Trains an Inception v3 network to regress joint co-ordinates (NUM_JOINTS
    sets of (x, y) co-ordinates) directly.
    """
    with tf.Graph().as_default():
        with tf.device('/cpu:0'):
            # TODO(brendan): Support multiple GPUs?
            assert FLAGS.num_gpus == 1

            training_batch = setup_train_input_pipeline(
                FLAGS.data_dir,
                FLAGS.num_readers,
                FLAGS.input_queue_memory_factor,
                FLAGS.batch_size,
                FLAGS.num_preprocess_threads,
                FLAGS.image_dim)

            global_step, optimizer = _setup_optimizer(FLAGS.batch_size,
                                                      FLAGS.num_epochs_per_decay,
                                                      FLAGS.initial_learning_rate,
                                                      FLAGS.learning_rate_decay_factor)

            train_op = _setup_training_op(training_batch,
                                          NUM_JOINT_COORDS,
                                          global_step,
                                          optimizer)

            # TODO(brendan): track moving averages of trainable variables

            tf_logging._logger.setLevel(INFO)

            slim.learning.train(
                train_op=train_op,
                logdir=FLAGS.log_dir,
                log_every_n_steps=10,
                global_step=global_step,
                session_config=tf.ConfigProto(allow_soft_placement=True))


def main(argv=None):
    """Usage: python3 -m train
    (After running write_tf_record.py. See its docstring for usage.)

    See top of this file for flags, e.g. --log_dir=./log, or type
    'python3 -m train --help' for options.
    """
    train()


if __name__ == "__main__":
    tf.app.run()