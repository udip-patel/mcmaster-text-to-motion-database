# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ==============================================================================
"""Contains model definitions for versions of the Oxford VGG network.

These model definitions were introduced in the following technical report:

  Very Deep Convolutional Networks For Large-Scale Image Recognition
  Karen Simonyan and Andrew Zisserman
  arXiv technical report, 2015
  PDF: http://arxiv.org/pdf/1409.1556.pdf
  ILSVRC 2014 Slides: http://www.robots.ox.ac.uk/~karen/pdf/ILSVRC_2014.pdf
  CC-BY-4.0

More information can be obtained from the VGG website:
www.robots.ox.ac.uk/~vgg/research/very_deep/

Usage:
  with slim.arg_scope(vgg.vgg_arg_scope()):
    outputs, end_points = vgg.vgg_16(inputs)

@@vgg_16
"""
from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import tensorflow as tf

slim = tf.contrib.slim



def bulat_vgg_arg_scope(weight_decay=0.0005):
    """Defines the VGG arg scope.

    Args:
      weight_decay: The l2 regularization coefficient.

    Returns:
      An arg_scope.
    """
    with slim.arg_scope([slim.conv2d, slim.fully_connected],
                        activation_fn=tf.nn.relu,
                        weights_regularizer=slim.l2_regularizer(weight_decay),
                        biases_initializer=tf.zeros_initializer):
      with slim.arg_scope([slim.conv2d], padding='SAME') as arg_sc:
        return arg_sc


def detector_vgg(inputs,
                 num_classes=16,
                 is_training=True,
                 dropout_keep_prob=0.5,
                 scope='detection_vgg_16'):
    """Oxford Net VGG 16-Layers Fully Convolutional with Skip-Connections as in the paper 'Fully Convolutional Networks
    for Semantic Segmentation' by Long et al.

    Args:
      inputs: a tensor of size [batch_size, height, width, channels].
      num_classes: number of predicted classes.
      is_training: whether or not the model is being trained. This is needed to set and unset dropout
      dropout_keep_prob: the probability that activations are kept in the dropout
        layers during training.
      spatial_squeeze: whether or not should squeeze the spatial dimensions of the
        outputs. Useful to remove unnecessary dimensions for classification.
      scope: Optional scope for the variables.

    Returns:
      the last op containing the log predictions and end_points dict.
    """
    with tf.variable_scope(scope, 'detection_vgg_16', [inputs]) as sc:
        end_points_collection = sc.name + '_end_points'
        # Collect outputs for conv2d, fully_connected and max_pool2d.
        with slim.arg_scope([slim.conv2d, slim.max_pool2d],
                            outputs_collections=end_points_collection):
            # A1
            # Dim(inputs) = [batch_size, 380, 380, 3]
            a1 = slim.repeat(inputs, 2, slim.conv2d, num_outputs=64,kernel_size= [3, 3], stride=[1,1], scope='conv1')
            # Dim(a1) = [batch_size, 380, 380, 64]
            a1 = slim.max_pool2d(a1, [1, 1], scope='pool1')
            # Dim(a1) = [batch_size, 190, 190, 64]

            # A2
            a2 = slim.repeat(a1, 2, slim.conv2d, 128, [3, 3], [1,1], scope='conv2')
            # Dim(a2) = [batch_size, 190, 190, 128]
            a2 = slim.max_pool2d(a2, [1, 1], scope='pool2')
            # Dim(a2) = [batch_size, 95, 95, 128]

            # A3 - We send the output of this to A8 and then add it to A9
            a3 = slim.repeat(a2, 3, slim.conv2d, 256, [3, 3], scope='conv3')
            # Dim(a3) = [batch_size, 95, 95, 256]
            a3 = slim.max_pool2d(a1, [1, 1], scope='pool3')
            # Dim(a3) = [batch_size, 48, 48, 256]

            # A4 - Also forms a skip connection - we need to send this output to A8 and then add this to A9
            a4 = slim.repeat(a3, 3, slim.conv2d, 512, [3, 3], scope='conv4')
            # Dim(a4) = [batch_size, 48, 48, 512]
            a4 = slim.max_pool2d(a4, [1, 1], scope='pool4')
            # Dim(a4) = [batch_size, 24, 24, 512]

            # A5
            a5 = slim.repeat(a4, 3, slim.conv2d, 512, [1, 1], scope='conv5')
            # Dim(a5) = [batch_size, 24, 24, 512]
            a5 = slim.max_pool2d(a5, [1, 1], scope='pool5')
            # Dim(a5) = [batch_size, 12, 12, 512]

            # Use conv2d instead of fully_connected layers.
            a6 = slim.conv2d(a5, 4096, [7, 7], padding='SAME', scope='fc6')
            a6 = slim.dropout(a6, dropout_keep_prob, is_training=is_training,
                               scope='dropout6')
            a7 = slim.conv2d(a6, 4096, [1, 1], scope='fc7')
            a7 = slim.dropout(a7, dropout_keep_prob, is_training=is_training,
                               scope='dropout7')
            a8 = slim.conv2d(a7, num_classes, [1, 1],
                              activation_fn=None,
                              normalizer_fn=None,
                              scope='fc8')

            a9 = tf.image.resize_bilinear(a8, [24, 24]) # Note that this makes sense wrt a4
            a4_skip = slim.conv2d(a4, num_classes, [1, 1], activation_fn=None, normalizer_fn=None,scope='a3_skip')
            a9 = a9 + a4_skip

            a9 = tf.image.resize_bilinear(a9, [48, 48]) # Note that this makes sense wrt a5
            a3_skip = slim.conv2d(a3, num_classes, [1, 1], activation_fn=None, normalizer_fn=None,scope='a4_skip')
            a9 = a9 + a3_skip

            # This needs to be 380 x 380 so we can stack it with the input image
            a9 = tf.image.resize_bilinear(a9,[380, 380])

            # Convert end_points_collection into a end_point dict.
            end_points = slim.utils.convert_collection_to_dict(end_points_collection)
            return a9, end_points


def regressor_vgg(num_classes=16,
                          is_training=True,
                          dropout_keep_prob=0.5,
                          scope='regression_vgg_16'):
    '''Args:
      inputs: a tensor of size [batch_size, height, width, channels].
      num_classes: number of predicted classes.
      is_training: whether or not the model is being trained. This is needed to set and unset dropout
      dropout_keep_prob: the probability that activations are kept in the dropout
        layers during training.
      scope: Optional scope for the variables.

    Returns:
      the last op containing the log predictions and end_points dict.
    '''
    with tf.variable_scope(scope, 'regression_vgg_16', [inputs]) as sc:
        end_points_collection = sc.name + '_end_points'
        # Collect outputs for conv2d, fully_connected and max_pool2d.
        with slim.arg_scope([slim.conv2d, slim.max_pool2d],
                            outputs_collections=end_points_collection):
            c1 = slim.conv2d(inputs, num_outputs=64, kernel_size=[9, 9], scope='conv1')
            c2 = slim.conv2d(c1, num_outputs=64, kernel_size=[13, 13], scope='conv2')
            c3 = slim.conv2d(c2, num_outputs=128, kernel_size=[13, 13], scope='conv3')
            c4 = slim.conv2d(c3, num_outputs=256, kernel_size=[15, 15], scope='conv4')
            c5 = slim.conv2d(c4, num_outputs=512, kernel_size=[1, 1], scope='conv5')
            c6 = slim.conv2d(c5, num_outputs=512, kernel_size=[1, 1], scope='conv6')
            c7 = slim.conv2d(c6, num_outputs=16, kernel_size=[1, 1], scope='conv7')
            c8 = slim.conv2d_transpose(c7, num_outputs=16, kernel_size=[8, 8], stride=[4, 4], scope='conv8')
            end_points = slim.utils.convert_collection_to_dict(end_points_collection)
            return end_points

bulat_vgg.default_image_size = 380
