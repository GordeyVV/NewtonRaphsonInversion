# Code is based on ReNoise https://github.com/garibida/ReNoise-Inversion

import torch
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

from diffusers import (
    StableDiffusionXLImg2ImgPipeline,
)
from diffusers.utils.torch_utils import randn_tensor

from diffusers.pipelines.stable_diffusion_xl.pipeline_stable_diffusion_xl import (
    StableDiffusionXLPipelineOutput,
    retrieve_timesteps,
    PipelineImageInput
)

#from src.eunms import Epsilon_Update_Type


def _backward_ddim(x_tm1, alpha_t, alpha_tm1, eps_xt):
    """
    let a = alpha_t, b = alpha_{t - 1}
    We have a > b,
    x_{t} - x_{t - 1} = sqrt(a) ((sqrt(1/b) - sqrt(1/a)) * x_{t-1} + (sqrt(1/a - 1) - sqrt(1/b - 1)) * eps_{t-1})
    From https://arxiv.org/pdf/2105.05233.pdf, section F.
    """

    a, b = alpha_t, alpha_tm1
    sa = a ** 0.5
    sb = b ** 0.5

    return sa * ((1 / sb) * x_tm1 + ((1 / a - 1) ** 0.5 - (1 / b - 1) ** 0.5) * eps_xt)


class SDXLDDIMPipeline(StableDiffusionXLImg2ImgPipeline):
    # @torch.no_grad()
    def __call__(
            self,
            prompt: Union[str, List[str]] = None,
            prompt_2: Optional[Union[str, List[str]]] = None,
            image: PipelineImageInput = None,
            strength: float = 0.3,
            num_inversion_steps: int = 50,
            timesteps: List[int] = None,
            denoising_start: Optional[float] = None,
            denoising_end: Optional[float] = None,
            guidance_scale: float = 1.0,
            negative_prompt: Optional[Union[str, List[str]]] = None,
            negative_prompt_2: Optional[Union[str, List[str]]] = None,
            num_images_per_prompt: Optional[int] = 1,
            eta: float = 0.0,
            generator: Optional[Union[torch.Generator, List[torch.Generator]]] = None,
            latents: Optional[torch.FloatTensor] = None,
            prompt_embeds: Optional[torch.FloatTensor] = None,
            negative_prompt_embeds: Optional[torch.FloatTensor] = None,
            pooled_prompt_embeds: Optional[torch.FloatTensor] = None,
            negative_pooled_prompt_embeds: Optional[torch.FloatTensor] = None,
            ip_adapter_image: Optional[PipelineImageInput] = None,
            output_type: Optional[str] = "pil",
            return_dict: bool = True,
            cross_attention_kwargs: Optional[Dict[str, Any]] = None,
            guidance_rescale: float = 0.0,
            original_size: Tuple[int, int] = None,
            crops_coords_top_left: Tuple[int, int] = (0, 0),
            target_size: Tuple[int, int] = None,
            negative_original_size: Optional[Tuple[int, int]] = None,
            negative_crops_coords_top_left: Tuple[int, int] = (0, 0),
            negative_target_size: Optional[Tuple[int, int]] = None,
            aesthetic_score: float = 6.0,
            negative_aesthetic_score: float = 2.5,
            clip_skip: Optional[int] = None,
            callback_on_step_end: Optional[Callable[[int, int, Dict], None]] = None,
            callback_on_step_end_tensor_inputs: List[str] = ["latents"],
            num_inference_steps: int = 50,
            inv_hp=None,
            **kwargs,
    ):
        callback = kwargs.pop("callback", None)
        callback_steps = kwargs.pop("callback_steps", None)

        if callback is not None:
            deprecate(
                "callback",
                "1.0.0",
                "Passing `callback` as an input argument to `__call__` is deprecated, consider use `callback_on_step_end`",
            )
        if callback_steps is not None:
            deprecate(
                "callback_steps",
                "1.0.0",
                "Passing `callback_steps` as an input argument to `__call__` is deprecated, consider use `callback_on_step_end`",
            )

        # 1. Check inputs. Raise error if not correct
        self.check_inputs(
            prompt,
            prompt_2,
            strength,
            num_inversion_steps,
            callback_steps,
            negative_prompt,
            negative_prompt_2,
            prompt_embeds,
            negative_prompt_embeds,
            callback_on_step_end_tensor_inputs,
        )

        denoising_start_fr = 1.0 - denoising_start
        denoising_start = denoising_start

        self._guidance_scale = guidance_scale
        self._guidance_rescale = guidance_rescale
        self._clip_skip = clip_skip
        self._cross_attention_kwargs = cross_attention_kwargs
        self._denoising_end = denoising_end
        self._denoising_start = denoising_start

        # 2. Define call parameters
        if prompt is not None and isinstance(prompt, str):
            batch_size = 1
        elif prompt is not None and isinstance(prompt, list):
            batch_size = len(prompt)
        else:
            batch_size = prompt_embeds.shape[0]

        device = self._execution_device

        # 3. Encode input prompt
        text_encoder_lora_scale = (
            self.cross_attention_kwargs.get("scale", None) if self.cross_attention_kwargs is not None else None
        )
        (
            prompt_embeds,
            negative_prompt_embeds,
            pooled_prompt_embeds,
            negative_pooled_prompt_embeds,
        ) = self.encode_prompt(
            prompt=prompt,
            prompt_2=prompt_2,
            device=device,
            num_images_per_prompt=num_images_per_prompt,
            do_classifier_free_guidance=self.do_classifier_free_guidance,
            negative_prompt=negative_prompt,
            negative_prompt_2=negative_prompt_2,
            prompt_embeds=prompt_embeds,
            negative_prompt_embeds=negative_prompt_embeds,
            pooled_prompt_embeds=pooled_prompt_embeds,
            negative_pooled_prompt_embeds=negative_pooled_prompt_embeds,
            lora_scale=text_encoder_lora_scale,
            clip_skip=self.clip_skip,
        )

        # 4. Preprocess image
        image = self.image_processor.preprocess(image)

        # 5. Prepare timesteps
        def denoising_value_valid(dnv):
            return isinstance(self.denoising_end, float) and 0 < dnv < 1

        timesteps, num_inversion_steps = retrieve_timesteps(self.scheduler, num_inversion_steps, device, timesteps)
        timesteps_num_inference_steps, num_inference_steps = retrieve_timesteps(self.scheduler_inference,
                                                                                num_inference_steps, device, None)

        timesteps, num_inversion_steps = self.get_timesteps(
            num_inversion_steps,
            strength,
            device,
            denoising_start=self.denoising_start if denoising_value_valid else None,
        )
        # latent_timestep = timesteps[:1].repeat(batch_size * num_images_per_prompt)

        # add_noise = True if self.denoising_start is None else False
        # 6. Prepare latent variables
        with torch.no_grad():
            latents = self.prepare_latents(
                image,
                None,
                batch_size,
                num_images_per_prompt,
                prompt_embeds.dtype,
                device,
                generator,
                False,
            )
        # 7. Prepare extra step kwargs.
        extra_step_kwargs = self.prepare_extra_step_kwargs(generator, eta)

        height, width = latents.shape[-2:]
        height = height * self.vae_scale_factor
        width = width * self.vae_scale_factor

        original_size = original_size or (height, width)
        target_size = target_size or (height, width)

        # 8. Prepare added time ids & embeddings
        if negative_original_size is None:
            negative_original_size = original_size
        if negative_target_size is None:
            negative_target_size = target_size

        add_text_embeds = pooled_prompt_embeds
        if self.text_encoder_2 is None:
            text_encoder_projection_dim = int(pooled_prompt_embeds.shape[-1])
        else:
            text_encoder_projection_dim = self.text_encoder_2.config.projection_dim

        add_time_ids, add_neg_time_ids = self._get_add_time_ids(
            original_size,
            crops_coords_top_left,
            target_size,
            aesthetic_score,
            negative_aesthetic_score,
            negative_original_size,
            negative_crops_coords_top_left,
            negative_target_size,
            dtype=prompt_embeds.dtype,
            text_encoder_projection_dim=text_encoder_projection_dim,
        )
        add_time_ids = add_time_ids.repeat(batch_size * num_images_per_prompt, 1)

        if self.do_classifier_free_guidance:
            prompt_embeds = torch.cat([negative_prompt_embeds, prompt_embeds], dim=0)
            add_text_embeds = torch.cat([negative_pooled_prompt_embeds, add_text_embeds], dim=0)
            add_neg_time_ids = add_neg_time_ids.repeat(batch_size * num_images_per_prompt, 1)
            add_time_ids = torch.cat([add_neg_time_ids, add_time_ids], dim=0)

        prompt_embeds = prompt_embeds.to(device)
        add_text_embeds = add_text_embeds.to(device)
        add_time_ids = add_time_ids.to(device)

        if ip_adapter_image is not None:
            image_embeds, negative_image_embeds = self.encode_image(ip_adapter_image, device, num_images_per_prompt)
            if self.do_classifier_free_guidance:
                image_embeds = torch.cat([negative_image_embeds, image_embeds])
                image_embeds = image_embeds.to(device)

        # 9. Denoising loop
        num_warmup_steps = max(len(timesteps) - num_inversion_steps * self.scheduler.order, 0)
        prev_timestep = None

        self._num_timesteps = len(timesteps)
        self.prev_z = torch.clone(latents)
        self.prev_z4 = torch.clone(latents)
        self.z_0 = torch.clone(latents)
        g_cpu = torch.Generator().manual_seed(7865)
        self.noise = randn_tensor(self.z_0.shape, generator=g_cpu, device=self.z_0.device, dtype=self.z_0.dtype)

        # Friendly inversion params
        timesteps_for = reversed(timesteps)
        noise = randn_tensor(latents.shape, generator=g_cpu, device=latents.device, dtype=latents.dtype)
        # latents = latents
        z_T = latents.clone()

        all_latents = [latents.clone()]
        with self.progress_bar(total=num_inversion_steps) as progress_bar:
            for i, t in enumerate(timesteps_for):

                added_cond_kwargs = {"text_embeds": add_text_embeds, "time_ids": add_time_ids}
                if ip_adapter_image is not None:
                    added_cond_kwargs["image_embeds"] = image_embeds

                z_tp1 = self.inversion_step(latents,
                                            t,
                                            prompt_embeds,
                                            added_cond_kwargs,
                                            prev_timestep=prev_timestep,
                                            inv_hp=inv_hp,
                                            z_0=self.z_0)

                prev_timestep = t
                latents = z_tp1

                all_latents.append(latents.clone())

                if callback_on_step_end is not None:
                    callback_kwargs = {}
                    for k in callback_on_step_end_tensor_inputs:
                        callback_kwargs[k] = locals()[k]
                    callback_outputs = callback_on_step_end(self, i, t, callback_kwargs)

                    latents = callback_outputs.pop("latents", latents)
                    prompt_embeds = callback_outputs.pop("prompt_embeds", prompt_embeds)
                    negative_prompt_embeds = callback_outputs.pop("negative_prompt_embeds", negative_prompt_embeds)
                    add_text_embeds = callback_outputs.pop("add_text_embeds", add_text_embeds)
                    negative_pooled_prompt_embeds = callback_outputs.pop(
                        "negative_pooled_prompt_embeds", negative_pooled_prompt_embeds
                    )
                    add_time_ids = callback_outputs.pop("add_time_ids", add_time_ids)
                    add_neg_time_ids = callback_outputs.pop("add_neg_time_ids", add_neg_time_ids)

                # call the callback, if provided
                if i == len(timesteps) - 1 or ((i + 1) > num_warmup_steps and (i + 1) % self.scheduler.order == 0):
                    progress_bar.update()
                    if callback is not None and i % callback_steps == 0:
                        step_idx = i // getattr(self.scheduler, "order", 1)
                        callback(step_idx, t, latents)

        image = latents

        # Offload all models
        self.maybe_free_model_hooks()

        return StableDiffusionXLPipelineOutput(images=image), all_latents

    def get_timestamp_dist(self, z_0, timesteps):
        timesteps = timesteps.to(z_0.device)
        sigma = self.scheduler.sigmas.cuda()[:-1][self.scheduler.timesteps == timesteps]
        z_0 = z_0.reshape(-1, 1)

        def gaussian_pdf(x):
            shape = x.shape
            x = x.reshape(-1, 1)
            all_probs = - 0.5 * torch.pow(((x - z_0) / sigma), 2)
            return all_probs.reshape(shape)

        return gaussian_pdf

    '''
    def inversion_step(
            self,
            z_t: torch.tensor,
            t: torch.tensor,
            prompt_embeds,
            added_cond_kwargs,
            prev_timestep: Optional[torch.tensor] = None,
            inv_hp=None,
            z_0=None,
    ) -> torch.tensor:

        n_iters, alpha, lr = inv_hp
        latent = z_t
        best_latent = None
        best_score = torch.inf
        curr_dist = self.get_timestamp_dist(z_0, t)
        for i in range(n_iters):
            latent.requires_grad = True
            noise_pred = self.unet_pass(latent, t, prompt_embeds, added_cond_kwargs)

            next_latent = self.backward_step(noise_pred, t, z_t, prev_timestep)
            f_x = (next_latent - latent).abs() - alpha * curr_dist(next_latent)
            l = f_x.sum()
            score = f_x.mean()

            if score < best_score:
                best_score = score
                best_latent = next_latent.detach()

            l.backward()
            latent = latent - (1 / (64 * 64 * 4)) * (l / latent.grad)
            latent.grad = None
            latent._grad_fn = None

        return best_latent
    '''
    
    # Levenberg-Marquardt with adapting damp param
    def inversion_step(
            self,
            z_t: torch.tensor,
            t: torch.tensor,
            prompt_embeds,
            added_cond_kwargs,
            prev_timestep: Optional[torch.tensor] = None,
            inv_hp=None,
            z_0=None,
    ) -> torch.tensor:

        n_iters, alpha, lr = inv_hp
        lambda_damp = 1e3  # Инициализация демпфирующего множителя
        lambda_up_factor = 10  # Множитель для увеличения lambda
        lambda_down_factor = 10  # Множитель для уменьшения lambda

        latent = z_t
        best_latent = None
        best_score = torch.inf
        curr_dist = self.get_timestamp_dist(z_0, t)
        for i in range(n_iters):
            latent.requires_grad = True
            noise_pred = self.unet_pass(latent, t, prompt_embeds, added_cond_kwargs)

            next_latent = self.backward_step(noise_pred, t, z_t, prev_timestep)
            f_x = (next_latent - latent).abs() - alpha * curr_dist(next_latent)
            l = f_x.sum()
            score = f_x.mean()

            l.backward()
            I = torch.eye(latent.size(0), device=latent.device)
            grad = latent.grad
            hessian_approx = grad @ grad.mT 

            # Обновление с демпфирующим множителем
            update = torch.inverse(hessian_approx + lambda_damp * I) @ grad

            if score < best_score:
                best_score = score
                best_latent = next_latent.detach()
                lambda_damp /= lambda_down_factor  # Уменьшение lambda_damp если шаг улучшил функцию потерь
            else:
                lambda_damp *= lambda_up_factor  # Увеличение lambda_damp если шаг не уменьшил функцию потерь

            latent = latent - lr * update
            latent.grad = None
            latent._grad_fn = None

        return best_latent
    
    
    @torch.no_grad()
    def unet_pass(self, z_t, t, prompt_embeds, added_cond_kwargs):
        latent_model_input = torch.cat([z_t] * 2) if self.do_classifier_free_guidance else z_t
        latent_model_input = self.scheduler.scale_model_input(latent_model_input, t)
        return self.unet(
            latent_model_input,
            t,
            encoder_hidden_states=prompt_embeds,
            timestep_cond=None,
            cross_attention_kwargs=self.cross_attention_kwargs,
            added_cond_kwargs=added_cond_kwargs,
            return_dict=False,
        )[0]

    @torch.no_grad()
    def backward_step(self, nosie_pred, t, z_t, prev_timestep):
        extra_step_kwargs = {}
        return self.scheduler.inv_step(nosie_pred, t, z_t, **extra_step_kwargs, return_dict=False)[0].detach()
