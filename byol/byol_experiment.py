# Copyright 2020 DeepMind Technologies Limited.
#
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""BYOL pre-training implementation.

Use this experiment to pre-train a self-supervised representation.
"""

import functools
from typing import Any, Generator, Mapping, NamedTuple, Text, Tuple, Union

from absl import logging
from acme.jax import utils as acme_utils
import haiku as hk
import jax
import jax.numpy as jnp
import numpy as np
import optax

from byol.utils import augmentations
from byol.utils import checkpointing
from byol.utils import dataset
from byol.utils import helpers
from byol.utils import networks
from byol.utils import optimizers
from byol.utils import schedules


# Type declarations.
LogsDict = Mapping[Text, jnp.ndarray]


class _ByolExperimentState(NamedTuple):
  """Byol's model and optimization parameters and state."""
  online_params: hk.Params
  target_params: hk.Params
  online_state: hk.State
  target_state: hk.State
  opt_state: optimizers.LarsState


class ByolExperiment:
  """Byol's training and evaluation component definition."""

  def __init__(
      self,
      random_seed: int,
      num_classes: int,
      batch_size: int,
      max_steps: int,
      enable_double_transpose: bool,
      base_target_ema: float,
      network_config: Mapping[Text, Any],
      optimizer_config: Mapping[Text, Any],
      lr_schedule_config: Mapping[Text, Any],
      evaluation_config: Mapping[Text, Any],
      checkpointing_config: Mapping[Text, Any],
      disable_momentum: bool = False,
      bp_proj: bool = False,
      loss: str = 'nc',
      **kwargs,
      ):
    """Constructs the experiment.

    Args:
      random_seed: the random seed to use when initializing network weights.
      num_classes: the number of classes; used for the online evaluation.
      batch_size: the total batch size; should be a multiple of the number of
        available accelerators.
      max_steps: the number of training steps; used for the lr/target network
        ema schedules.
      enable_double_transpose: see dataset.py; only has effect on TPU.
      base_target_ema: the initial value for the ema decay rate of the target
        network.
      network_config: the configuration for the network.
      optimizer_config: the configuration for the optimizer.
      lr_schedule_config: the configuration for the learning rate schedule.
      evaluation_config: the evaluation configuration.
      checkpointing_config: the configuration for checkpointing.
    """

    self._random_seed = random_seed
    self._enable_double_transpose = enable_double_transpose
    self._num_classes = num_classes
    self._lr_schedule_config = lr_schedule_config
    self._batch_size = batch_size
    self._max_steps = max_steps
    self._base_target_ema = base_target_ema
    self._optimizer_config = optimizer_config
    self._evaluation_config = evaluation_config
    self._disable_momentum = disable_momentum
    self._bp_proj = bp_proj
    self._loss = loss

    # Checkpointed experiment state.
    self._byol_state = None

    # Input pipelines.
    self._train_input = None
    self._eval_input = None

    # build the transformed ops
    forward_fn = functools.partial(self._forward, **network_config)
    self.forward = hk.without_apply_rng(hk.transform_with_state(forward_fn))
    # training can handle multiple devices, thus the pmap
    self.update_pmap = jax.pmap(self._update_fn, axis_name='i')
    # evaluation can only handle single device
    self.eval_batch_jit = jax.jit(self._eval_batch)

    self._checkpointer = checkpointing.Checkpointer(**checkpointing_config)

  def _forward(
      self,
      inputs: dataset.Batch,
      projector_hidden_size: int,
      projector_output_size: int,
      predictor_hidden_size: int,
      encoder_class: Text,
      encoder_config: Mapping[Text, Any],
      bn_config: Mapping[Text, Any],
      is_training: bool,
      predictor_mode: str,
  ) -> Mapping[Text, jnp.ndarray]:
    """Forward application of byol's architecture.

    Args:
      inputs: A batch of data, i.e. a dictionary, with either two keys,
        (`images` and `labels`) or three keys (`view1`, `view2`, `labels`).
      projector_hidden_size: hidden size of the projector MLP.
      projector_output_size: output size of the projector and predictor MLPs.
      predictor_hidden_size: hidden size of the predictor MLP.
      encoder_class: type of the encoder (should match a class in
        utils/networks).
      encoder_config: passed to the encoder constructor.
      bn_config: passed to the hk.BatchNorm constructors.
      is_training: Training or evaluating the model? When True, inputs must
        contain keys `view1` and `view2`. When False, inputs must contain key
        `images`.

    Returns:
      All outputs of the model, i.e. a dictionary with projection, prediction
      and logits keys, for either the two views, or the image.
    """
    encoder = getattr(networks, encoder_class)
    net = encoder(
        num_classes=None,  # Don't build the final linear layer
        bn_config=bn_config,
        **encoder_config)

    if self._bp_proj:
      projector = networks.MLPHT(
          name='projector',
          hidden_size=projector_hidden_size,
          output_size=projector_output_size,
      )
    else:
      projector = networks.MLP(
          name='projector',
          hidden_size=projector_hidden_size,
          output_size=projector_output_size,
          bn_config=bn_config)
    predictor = networks.MLP(
        name='predictor',
        hidden_size=predictor_hidden_size,
        output_size=projector_output_size,
        bn_config=bn_config)
    classifier = hk.Linear(
        output_size=self._num_classes, name='classifier')
    closed_form_predictor_ema_1 = hk.get_state('closed_form_predictor_ema_1', shape=[projector_output_size,projector_output_size], dtype=jnp.float32, init=jnp.zeros)
    closed_form_predictor_ema_2 = hk.get_state('closed_form_predictor_ema_2', shape=[projector_output_size,projector_output_size], dtype=jnp.float32, init=jnp.zeros)

    def apply_once_fn(images: jnp.ndarray, suffix: Text = ''):
      images = dataset.normalize_images(images)

      embedding = net(images, is_training=is_training)
      proj_out = projector(embedding, is_training)
      pred_out = predictor(proj_out, is_training)

      # Note the stop_gradient: label information is not leaked into the
      # main network.
      classif_out = classifier(jax.lax.stop_gradient(embedding))
      outputs = {}
      outputs['projection' + suffix] = proj_out
      outputs['prediction' + suffix] = pred_out
      outputs['logits' + suffix] = classif_out
      return outputs
    
    def apply_twice_fn(image1: jnp.ndarray, image2: jnp.ndarray):
      image1 = dataset.normalize_images(image1)
      image2 = dataset.normalize_images(image2)

      embedding1 = net(image1, is_training=is_training)
      embedding2 = net(image2, is_training=is_training)
      proj_out1 = projector(embedding1, is_training)
      proj_out2 = projector(embedding2, is_training)

      pred_out1 = predictor(proj_out1, is_training)
      pred_out2 = predictor(proj_out2, is_training)

      # Note the stop_gradient: label information is not leaked into the
      # main network.
      classif_out1 = classifier(jax.lax.stop_gradient(embedding1))
      classif_out2 = classifier(jax.lax.stop_gradient(embedding2))
      outputs = {}
      outputs['projection_view1'] = proj_out1
      outputs['projection_view2'] = proj_out2
      outputs['prediction_view1'] = pred_out1
      outputs['prediction_view2'] = pred_out2
      outputs['logits_view1'] = classif_out1
      outputs['logits_view2'] = classif_out2
      return outputs
    
    def ne_predictor(zt, zx):
      zttzx = jax.numpy.matmul(jax.numpy.transpose(zt), zx)
      p = 2.0 * zttzx
      p -= jax.numpy.matmul( jax.numpy.transpose(zt), jax.numpy.matmul(zt, zttzx))
      return p
    
    def visser(m, n, eta):
      p = 1/(2.0 * eta) * jax.numpy.eye(m.shape[0])
      for _ in range(n):
        p += eta * (m - jax.numpy.matmul(p, p))
      return p
    
    def newtonschulz(m, n):
      a=m
      fronorm_a = jax.numpy.linalg.norm(a, 'fro') 
      a /= fronorm_a
      b = jax.numpy.eye(m.shape[0])
      c = 3.0 * b
      for _ in range(n):
        ba = jax.numpy.matmul(b, a)
        a = 0.5 * jax.numpy.matmul(a, c - ba)
        b = 0.5 * jax.numpy.matmul(c - ba, b)
      a *= jax.numpy.sqrt(fronorm_a)
      b /= jax.numpy.sqrt(fronorm_a)
      return a, b
    
    def stiefel(m, n):
      x = m.transpose()
      xxt = jax.numpy.matmul(x, m)
      xxt /= m.shape[0]
      x_inv_sqrt = newtonschulz(xxt, n)[1]
      x_inv_sqrt /= jax.numpy.linalg.norm(x_inv_sqrt, ord='fro')
      p = jax.numpy.matmul(x_inv_sqrt, x)
      return p
    
    def lrp(zt, zx, safe_eps=1e-12):
      normfactor = 0.5 * jax.numpy.linalg.norm(zt, ord='fro')
      normfactor += 0.5 * jax.numpy.linalg.norm(zx, ord='fro') + safe_eps
      p = jax.numpy.matmul(jax.numpy.linalg.pinv(zt/normfactor), zx/normfactor)
      return p
    
    def direct_copy(zt):
      return jax.numpy.matmul(jax.numpy.transpose(zt), zt)
    
    def apply_twice_cfp_fn(image1: jnp.ndarray, image2: jnp.ndarray):
      image1 = dataset.normalize_images(image1)
      image2 = dataset.normalize_images(image2)

      embedding1 = net(image1, is_training=is_training)
      embedding2 = net(image2, is_training=is_training)
      proj_out1 = projector(embedding1, is_training)
      proj_out2 = projector(embedding2, is_training)

      if predictor_mode == 'byol':
        pred_out1 = predictor(proj_out1, is_training)
        pred_out2 = predictor(proj_out2, is_training)

      else:

        if predictor_mode == 'directcopy':
          closed_form_predictor1 = direct_copy(proj_out1)
          closed_form_predictor2 = direct_copy(proj_out2)

        # one version of LRP
        # closed_form_predictor1 = jax.numpy.linalg.inv(proj_out1.T @ proj_out1) @ proj_out1.T @ proj_out2
        # closed_form_predictor2 = jax.numpy.linalg.inv(proj_out2.T @ proj_out2) @ proj_out2.T @ proj_out1

        elif predictor_mode == 'lrp':
          # paper version of LRP
          closed_form_predictor1 = lrp(proj_out1, proj_out2)
          closed_form_predictor2 = lrp(proj_out2, proj_out1)


        elif predictor_mode == 'ne':
          # NE predictor
          z1 = proj_out1/jnp.linalg.norm(proj_out1, ord=2)
          z2 = proj_out2/jnp.linalg.norm(proj_out2, ord=2)
          p1 = ne_predictor(z1, z2)
          p2 = ne_predictor(z2, z1)
          closed_form_predictor1 = p1/jnp.linalg.norm(p1, ord=2)
          closed_form_predictor2 = p2/jnp.linalg.norm(p2, ord=2)

        elif predictor_mode == 'visser':
          # visser predictor
          sigma1 = jax.numpy.matmul(jax.numpy.transpose(proj_out1), proj_out1)
          sigma2 = jax.numpy.matmul(jax.numpy.transpose(proj_out2), proj_out2)
          closed_form_predictor1 = visser(sigma1, 50, 0.001)
          closed_form_predictor2 = visser(sigma2, 50, 0.001)

        elif predictor_mode == 'newtonschulz':
          # newtonschulz predictor
          sigma1 = jax.numpy.matmul(jax.numpy.transpose(proj_out1), proj_out1)
          sigma2 = jax.numpy.matmul(jax.numpy.transpose(proj_out2), proj_out2)
          closed_form_predictor1 = newtonschulz(sigma1, 9)[0]
          closed_form_predictor2 = newtonschulz(sigma2, 9)[0]

        elif predictor_mode == 'stiefel':
          # stiefel predictor
          sigma1 = jax.numpy.matmul(jax.numpy.transpose(proj_out1), proj_out1)
          sigma2 = jax.numpy.matmul(jax.numpy.transpose(proj_out2), proj_out2)
          closed_form_predictor1 = stiefel(sigma1, 9)
          closed_form_predictor2 = stiefel(sigma2, 9)

        else:
          raise ValueError('predictor_mode not recognized')


        ema_factor = 0.9
        epsilon = 0.3

        # use ema on predictors
        # hk.set_state('closed_form_predictor_ema_1', ema_factor * closed_form_predictor_ema_1 + (1-ema_factor) * closed_form_predictor1)
        # hk.set_state('closed_form_predictor_ema_2', ema_factor * closed_form_predictor_ema_2 + (1-ema_factor) * closed_form_predictor2)

        # predictor1 = closed_form_predictor_ema_1 + epsilon * jnp.eye(projector_output_size)
        # predictor2 = closed_form_predictor_ema_2 + epsilon * jnp.eye(projector_output_size)

        # no ema on predictors
        predictor1 = closed_form_predictor1 + epsilon * jnp.eye(projector_output_size)
        predictor2 = closed_form_predictor2 + epsilon * jnp.eye(projector_output_size)

        # use gradients on predictors
        # pred_out1 = proj_out1 @ predictor1
        # pred_out2 = proj_out2 @ predictor2

        # no gradients on predictors
        pred_out1 = proj_out1 @ jax.lax.stop_gradient(predictor1) 
        pred_out2 = proj_out2 @ jax.lax.stop_gradient(predictor2) 

      # Note the stop_gradient: label information is not leaked into the
      # main network.
      classif_out1 = classifier(jax.lax.stop_gradient(embedding1))
      classif_out2 = classifier(jax.lax.stop_gradient(embedding2))
      outputs = {}
      outputs['projection_view1'] = proj_out1
      outputs['projection_view2'] = proj_out2
      outputs['prediction_view1'] = pred_out1
      outputs['prediction_view2'] = pred_out2
      outputs['logits_view1'] = classif_out1
      outputs['logits_view2'] = classif_out2
      return outputs

    if is_training:
      # outputs_view1 = apply_once_fn(inputs['view1'], '_view1')  # pytype: disable=wrong-arg-types  # jax-ndarray
      # outputs_view2 = apply_once_fn(inputs['view2'], '_view2')  # pytype: disable=wrong-arg-types  # jax-ndarray
      # return {**outputs_view1, **outputs_view2}
      # return apply_twice_fn(inputs['view1'], inputs['view2'])
      return apply_twice_cfp_fn(inputs['view1'], inputs['view2'])
    else:
      return apply_once_fn(inputs['images'], '')  # pytype: disable=wrong-arg-types  # jax-ndarray

  def _optimizer(self, learning_rate: float) -> optax.GradientTransformation:
    """Build optimizer from config."""
    if self._optimizer_config['name'] == 'lars':
      return optimizers.lars(
          learning_rate,
          weight_decay_filter=optimizers.exclude_bias_and_norm,
          lars_adaptation_filter=optimizers.exclude_bias_and_norm,
          weight_decay=self._optimizer_config['weight_decay'],
          momentum=self._optimizer_config['momentum'],
          eta=self._optimizer_config['eta'],)
    elif self._optimizer_config['name'] == 'sgd':
      return optax.chain(
          optax.add_decayed_weights(self._optimizer_config['weight_decay']),
          optax.sgd(
            learning_rate=learning_rate,
            momentum=self._optimizer_config['momentum'])
            )
    elif self._optimizer_config['name'] == 'adam':
      return optax.adam(learning_rate=1e-3)
    else:
      raise ValueError(f'Unknown optimizer: {self._optimizer_config["name"]}')
    

      

  def loss_fn(
      self,
      online_params: hk.Params,
      target_params: hk.Params,
      online_state: hk.State,
      target_state: hk.Params,
      rng: jnp.ndarray,
      inputs: dataset.Batch,
  ) -> Tuple[jnp.ndarray, Tuple[Mapping[Text, hk.State], LogsDict]]:
    """Compute BYOL's loss function.

    Args:
      online_params: parameters of the online network (the loss is later
        differentiated with respect to the online parameters).
      target_params: parameters of the target network.
      online_state: internal state of online network.
      target_state: internal state of target network.
      rng: random number generator state.
      inputs: inputs, containing two batches of crops from the same images,
        view1 and view2 and labels

    Returns:
      BYOL's loss, a mapping containing the online and target networks updated
      states after processing inputs, and various logs.
    """
    if self._should_transpose_images():
      inputs = dataset.transpose_images(inputs)
    inputs = augmentations.postprocess(inputs, rng)
    labels = inputs['labels']

    online_network_out, online_state = self.forward.apply(
        params=online_params,
        state=online_state,
        inputs=inputs,
        is_training=True)
    target_network_out, target_state = self.forward.apply(
        params=target_params,
        state=target_state,
        inputs=inputs,
        is_training=True)

    # Representation loss

    # The stop_gradient is not necessary as we explicitly take the gradient with
    # respect to online parameters only in `optax.apply_updates`. We leave it to
    # indicate that gradients are not backpropagated through the target network.
    if self._loss == 'nc':
      regression_loss = helpers.normalized_l2_loss
    elif self._loss == 'l1':
      regression_loss = helpers.l1_loss
    else:
      raise ValueError(f'Unknown loss: {self._loss}')
    repr_loss = regression_loss(
        online_network_out['prediction_view1'],
        jax.lax.stop_gradient(target_network_out['projection_view2']))
    repr_loss = repr_loss + regression_loss(
        online_network_out['prediction_view2'],
        jax.lax.stop_gradient(target_network_out['projection_view1']))

    repr_loss = jnp.mean(repr_loss)

    # Classification loss (with gradient flows stopped from flowing into the
    # ResNet). This is used to provide an evaluation of the representation
    # quality during training.

    classif_loss = helpers.softmax_cross_entropy(
        logits=online_network_out['logits_view1'],
        labels=jax.nn.one_hot(labels, self._num_classes))

    top1_correct = helpers.topk_accuracy(
        online_network_out['logits_view1'],
        inputs['labels'],
        topk=1,
    )

    top5_correct = helpers.topk_accuracy(
        online_network_out['logits_view1'],
        inputs['labels'],
        topk=5,
    )

    top1_acc = jnp.mean(top1_correct)
    top5_acc = jnp.mean(top5_correct)

    classif_loss = jnp.mean(classif_loss)
    loss = repr_loss + classif_loss
    logs = dict(
        loss=loss,
        repr_loss=repr_loss,
        classif_loss=classif_loss,
        top1_accuracy=top1_acc,
        top5_accuracy=top5_acc,
    )

    return loss, (dict(online_state=online_state,
                       target_state=target_state), logs)

  def _should_transpose_images(self):
    """Should we transpose images (saves host-to-device time on TPUs)."""
    return (self._enable_double_transpose and
            jax.local_devices()[0].platform == 'tpu')

  def _update_fn(
      self,
      byol_state: _ByolExperimentState,
      global_step: jnp.ndarray,
      rng: jnp.ndarray,
      inputs: dataset.Batch,
  ) -> Tuple[_ByolExperimentState, LogsDict]:
    """Update online and target parameters.

    Args:
      byol_state: current BYOL state.
      global_step: current training step.
      rng: current random number generator
      inputs: inputs, containing two batches of crops from the same images,
        view1 and view2 and labels

    Returns:
      Tuple containing the updated Byol state after processing the inputs, and
      various logs.
    """
    online_params = byol_state.online_params
    target_params = byol_state.target_params
    online_state = byol_state.online_state
    target_state = byol_state.target_state
    opt_state = byol_state.opt_state

    # update online network
    grad_fn = jax.grad(self.loss_fn, argnums=0, has_aux=True)
    grads, (net_states, logs) = grad_fn(online_params, target_params,
                                        online_state, target_state, rng, inputs)

    # cross-device grad and logs reductions
    grads = jax.tree_map(lambda v: jax.lax.pmean(v, axis_name='i'), grads)
    logs = jax.tree_map(lambda x: jax.lax.pmean(x, axis_name='i'), logs)

    learning_rate = schedules.learning_schedule(
        global_step,
        batch_size=self._batch_size,
        total_steps=self._max_steps,
        **self._lr_schedule_config)
    updates, opt_state = self._optimizer(learning_rate).update(
        grads, opt_state, online_params)
    online_params = optax.apply_updates(online_params, updates)

    # update target network
    tau = schedules.target_ema(
        global_step,
        base_ema=self._base_target_ema,
        max_steps=self._max_steps)
    if self._disable_momentum:
      tau = 0.
    target_params = jax.tree_map(lambda x, y: x + (1 - tau) * (y - x),
                                      target_params, online_params)
    logs['tau'] = tau
    logs['learning_rate'] = learning_rate
    return _ByolExperimentState(
        online_params=online_params,
        target_params=target_params,
        online_state=net_states['online_state'],
        target_state=net_states['target_state'],
        opt_state=opt_state), logs

  def _make_initial_state(
      self,
      rng: jnp.ndarray,
      dummy_input: dataset.Batch,
  ) -> _ByolExperimentState:
    """BYOL's _ByolExperimentState initialization.

    Args:
      rng: random number generator used to initialize parameters. If working in
        a multi device setup, this need to be a ShardedArray.
      dummy_input: a dummy image, used to compute intermediate outputs shapes.

    Returns:
      Initial Byol state.
    """
    rng_online, rng_target = jax.random.split(rng)

    if self._should_transpose_images():
      dummy_input = dataset.transpose_images(dummy_input)

    # Online and target parameters are initialized using different rngs,
    # in our experiments we did not notice a significant different with using
    # the same rng for both.
    online_params, online_state = self.forward.init(
        rng_online,
        dummy_input,
        is_training=True,
    )
    target_params, target_state = self.forward.init(
        rng_target,
        dummy_input,
        is_training=True,
    )
    opt_state = self._optimizer(0).init(online_params)
    return _ByolExperimentState(
        online_params=online_params,
        target_params=target_params,
        opt_state=opt_state,
        online_state=online_state,
        target_state=target_state,
    )

  def step(self, *,
           global_step: jnp.ndarray,
           rng: jnp.ndarray) -> Mapping[Text, np.ndarray]:
    """Performs a single training step."""
    if self._train_input is None:
      self._initialize_train()

    inputs = next(self._train_input)

    self._byol_state, scalars = self.update_pmap(
        self._byol_state,
        global_step=global_step,
        rng=rng,
        inputs=inputs,
    )

    return helpers.get_first(scalars)

  def save_checkpoint(self, step: int, rng: jnp.ndarray):
    self._checkpointer.maybe_save_checkpoint(
        self._byol_state, step=step, rng=rng, is_final=step >= self._max_steps)

  def load_checkpoint(self) -> Union[Tuple[int, jnp.ndarray], None]:
    checkpoint_data = self._checkpointer.maybe_load_checkpoint()
    if checkpoint_data is None:
      return None
    self._byol_state, step, rng = checkpoint_data
    return step, rng

  def _initialize_train(self):
    """Initialize train.

    This includes initializing the input pipeline and Byol's state.
    """
    self._train_input = acme_utils.prefetch(self._build_train_input())

    # Check we haven't already restored params
    if self._byol_state is None:
      logging.info(
          'Initializing parameters rather than restoring from checkpoint.')

      # initialize Byol and setup optimizer state
      inputs = next(self._train_input)
      init_byol = jax.pmap(self._make_initial_state, axis_name='i')

      # Init uses the same RNG key on all hosts+devices to ensure everyone
      # computes the same initial state and parameters.
      init_rng = jax.random.PRNGKey(self._random_seed)
      init_rng = helpers.bcast_local_devices(init_rng)

      self._byol_state = init_byol(rng=init_rng, dummy_input=inputs)

  def _build_train_input(self) -> Generator[dataset.Batch, None, None]:
    """Loads the (infinitely looping) dataset iterator."""
    num_devices = jax.device_count()
    global_batch_size = self._batch_size
    per_device_batch_size, ragged = divmod(global_batch_size, num_devices)

    if ragged:
      raise ValueError(
          f'Global batch size {global_batch_size} must be divisible by '
          f'num devices {num_devices}')

    return dataset.load(
        dataset.Split.TRAIN_AND_VALID,
        preprocess_mode=dataset.PreprocessMode.PRETRAIN,
        transpose=self._should_transpose_images(),
        batch_dims=[jax.local_device_count(), per_device_batch_size])

  def _eval_batch(
      self,
      params: hk.Params,
      state: hk.State,
      batch: dataset.Batch,
  ) -> Mapping[Text, jnp.ndarray]:
    """Evaluates a batch.

    Args:
      params: Parameters of the model to evaluate. Typically Byol's online
        parameters.
      state: State of the model to evaluate. Typically Byol's online state.
      batch: Batch of data to evaluate (must contain keys images and labels).

    Returns:
      Unreduced evaluation loss and top1 accuracy on the batch.
    """
    if self._should_transpose_images():
      batch = dataset.transpose_images(batch)

    outputs, _ = self.forward.apply(params, state, batch, is_training=False)
    logits = outputs['logits']
    labels = hk.one_hot(batch['labels'], self._num_classes)
    loss = helpers.softmax_cross_entropy(logits, labels, reduction=None)
    top1_correct = helpers.topk_accuracy(logits, batch['labels'], topk=1)
    top5_correct = helpers.topk_accuracy(logits, batch['labels'], topk=5)
    # NOTE: Returned values will be summed and finally divided by num_samples.
    return {
        'eval_loss': loss,
        'top1_accuracy': top1_correct,
        'top5_accuracy': top5_correct,
    }

  def _eval_epoch(self, subset: Text, batch_size: int):
    """Evaluates an epoch."""
    num_samples = 0.
    summed_scalars = None

    params = helpers.get_first(self._byol_state.online_params)
    state = helpers.get_first(self._byol_state.online_state)
    split = dataset.Split.from_string(subset)

    dataset_iterator = dataset.load(
        split,
        preprocess_mode=dataset.PreprocessMode.EVAL,
        transpose=self._should_transpose_images(),
        batch_dims=[batch_size])

    for inputs in dataset_iterator:
      num_samples += inputs['labels'].shape[0]
      scalars = self.eval_batch_jit(params, state, inputs)

      # Accumulate the sum of scalars for each step.
      scalars = jax.tree_map(lambda x: jnp.sum(x, axis=0), scalars)
      if summed_scalars is None:
        summed_scalars = scalars
      else:
        summed_scalars = jax.tree_map(jnp.add, summed_scalars, scalars)

    mean_scalars = jax.tree_map(lambda x: x / num_samples, summed_scalars)
    return mean_scalars

  def evaluate(self, global_step, **unused_args):
    """Thin wrapper around _eval_epoch."""

    global_step = np.array(helpers.get_first(global_step))
    scalars = jax.device_get(self._eval_epoch(**self._evaluation_config))

    logging.info('[Step %d] Eval scalars: %s', global_step, scalars)
    return scalars
