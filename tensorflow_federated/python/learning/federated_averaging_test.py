# Copyright 2018, The TensorFlow Federated Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Tests for learning.federated_averaging."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

# Dependency imports

from absl.testing import parameterized
import numpy as np
import tensorflow as tf

from tensorflow_federated.python.common_libs import test_utils
from tensorflow_federated.python.learning import federated_averaging
from tensorflow_federated.python.learning import model_examples
from tensorflow_federated.python.learning import model_utils
from tensorflow_federated.python.tensorflow_libs import tensor_utils


# Tests in this file default to eager-mode, but can use this decorator
# to use graph-mode.
def graph_mode_test(test_func):
  """Decorator for a test to be executed in graph mode.

  This introduces a default Graph, which tests annotated with
  @graph_mode_test may use or ignore by creating their own Graphs.

  Args:
    test_func: A test function to be decorated.

  Returns:
    The decorated test_func.
  """

  def wrapped_test_func(*args, **kwargs):
    with tf.Graph().as_default():
      test_func(*args, **kwargs)

  return wrapped_test_func


class FederatedAveragingTest(test_utils.TffTestCase, parameterized.TestCase):

  @graph_mode_test
  def test_client_tf(self):
    model = model_examples.TrainableLinearRegression(feature_dim=2)

    # Create a dataset with 4 examples:
    dataset = tf.data.Dataset.from_tensor_slices(
        model_examples.TrainableLinearRegression.make_batch(
            x=[[0.0, 0.0], [1.0, 0.0], [2.0, 0.0], [3.0, 0.0]],
            y=[0.0, 0.0, 1.0, 1.0]))
    # Repeat the dataset 5 times with batches of 3 examples,
    # producing 7 minibatches (the last one with only 2 examples).
    # Note thta `batch` is required for this dataset to be useable,
    # as it adds the batch dimension which is expected by the model.
    dataset = dataset.repeat(5).batch(3)

    initial_model = model_utils.ModelVars(
        trainable={
            'a': tf.constant([[0.0], [0.0]]),
            'b': tf.constant(0.0)
        },
        non_trainable={'c': 0.0})

    init_op = model_utils.model_initializer(model)
    client_outputs = federated_averaging.client_tf(model, dataset,
                                                   initial_model)

    tf.get_default_graph().finalize()
    with self.session() as sess:
      sess.run(init_op)
      out = sess.run(client_outputs)

      # Both trainable parameters should have been updated,
      # and we don't return the non-trainable 'c'.
      self.assertCountEqual(['a', 'b'], out.model_delta.keys())
      self.assertGreater(np.linalg.norm(out.model_delta['a']), 0.1)
      self.assertGreater(np.linalg.norm(out.model_delta['b']), 0.1)

      self.assertEqual(out.model_output['num_examples'], 20)
      self.assertEqual(out.model_output['num_batches'], 7)
      self.assertBetween(out.model_output['loss'],
                         np.finfo(np.float32).eps, 10.0)

  # pyformat: disable
  @parameterized.named_parameters(
      ('_sgd', lambda: tf.train.GradientDescentOptimizer(learning_rate=0.1),
       0.1, 0),
      # It looks like Adam introduces 2 + 2*num_model_variables additional vars.
      ('_adam', lambda: tf.train.AdamOptimizer(  # pylint: disable=g-long-lambda
          learning_rate=0.1, beta1=0.0, beta2=0.0, epsilon=1.0), 0.05, 6))
  # pyformat: enable
  def test_server_eager_mode(self, optimizer_fn, updated_val,
                             num_optimizer_vars):
    model_fn = lambda: model_examples.TrainableLinearRegression(feature_dim=2)

    server_state = federated_averaging.server_init(model_fn, optimizer_fn)
    train_vars = server_state.model.trainable
    self.assertAllClose(train_vars['a'].numpy(), np.array([[0.0], [0.0]]))
    self.assertEqual(train_vars['b'].numpy(), 0.0)
    self.assertEqual(server_state.model.non_trainable['c'].numpy(),
                     0.0)
    self.assertLen(server_state.optimizer_state, num_optimizer_vars)
    model_delta = tensor_utils.to_odict({
        'a': tf.constant([[1.0], [0.0]]),
        'b': tf.constant(1.0)
    })
    server_state = federated_averaging.server_update_model(
        server_state, model_delta, model_fn, optimizer_fn)

    train_vars = server_state.model.trainable
    # For SGD: learning_Rate=0.1, update=[1.0, 0.0], initial model=[0.0, 0.0],
    # so updated_val=0.1
    self.assertAllClose(train_vars['a'].numpy(), [[updated_val], [0.0]])
    self.assertAllClose(train_vars['b'].numpy(), updated_val)
    self.assertEqual(server_state.model.non_trainable['c'].numpy(), 0.0)

  @graph_mode_test
  def test_server_graph_mode(self):
    optimizer_fn = lambda: tf.train.GradientDescentOptimizer(learning_rate=0.1)
    model_fn = lambda: model_examples.TrainableLinearRegression(feature_dim=2)

    # Explicitly entering a graph as a default enables graph-mode.
    with tf.Graph().as_default() as g:
      server_state_op = federated_averaging.server_init(model_fn, optimizer_fn)
      init_op = tf.group(tf.global_variables_initializer(),
                         tf.local_variables_initializer())
      g.finalize()
      with self.session() as sess:
        sess.run(init_op)
        server_state = sess.run(server_state_op)
    train_vars = server_state.model.trainable
    self.assertAllClose(train_vars['a'], [[0.0], [0.0]])
    self.assertEqual(train_vars['b'], 0.0)
    self.assertEqual(server_state.model.non_trainable['c'], 0.0)
    self.assertEmpty(server_state.optimizer_state)

    with tf.Graph().as_default() as g:
      # N.B. Must use a fresh graph so variable names are the same.
      model_delta = tensor_utils.to_odict({
          'a': tf.constant([[1.0], [0.0]]),
          'b': tf.constant(2.0)
      })
      update_op = federated_averaging.server_update_model(
          server_state, model_delta, model_fn, optimizer_fn)
      init_op = tf.group(tf.global_variables_initializer(),
                         tf.local_variables_initializer())
      g.finalize()
      with self.session() as sess:
        sess.run(init_op)
        server_state = sess.run(update_op)
    train_vars = server_state.model.trainable
    # learning_Rate=0.1, update is [1.0, 0.0], initial model is [0.0, 0.0].
    self.assertAllClose(train_vars['a'], [[0.1], [0.0]])
    self.assertAllClose(train_vars['b'], 0.2)
    self.assertEqual(server_state.model.non_trainable['c'], 0.0)


if __name__ == '__main__':
  # We default to eager execution, and use the @graph_mode_test
  # annotation for graph-mode (sess.run) tests.
  tf.enable_eager_execution()
  tf.test.main()
