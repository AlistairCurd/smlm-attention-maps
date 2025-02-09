#!/usr/bin/env python3
import argparse
import io
from pathlib import Path
import sys
# import shutil
from typing import Dict, Tuple
from concurrent import futures
from urllib.parse import urlparse
import warnings


# loading all the below packages takes quite a bit of time, so get cli parsing
# out of the way beforehand so it's more responsive in case of errors
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Create heatmaps for MIL models."
        )
    parser.add_argument(
        "slide_urls",
        metavar="SLIDE_URL",
        type=urlparse,
        nargs="+",
        help="Slides to create heatmaps for.",
    )
    parser.add_argument(
        "-m",
        "--model-path",
        type=Path,
        required=True,
        help="MIL model used to generate attention / score maps.",
    )
    parser.add_argument(
        "-o", "--output-path",
        type=Path, required=True, help="Path to save results to."
    )
    parser.add_argument(
        "-t",
        "--true-class",
        type=str,
        required=True,
        help='Class to be rendered as "hot" in the heatmap.',
    )
    parser.add_argument(
        "--from-file",
        metavar="FILE",
        type=Path,
        help="File containing a list of slides to create heatmaps for.",
    )
    parser.add_argument(
        "--blur-kernel-size",
        metavar="SIZE",
        type=int,
        default=15,
        help="Size of gaussian pooling filter. 0 disables pooling.",
    )
    parser.add_argument(
        "--cache-dir",
        type=Path,
        default=None,
        help="Directory to cache extracted features etc. in.",
    )
    parser.add_argument(
        "--force-cpu",
        type=bool,
        default=False,
        help="Forcing the use of cpu regardless of cuda availability.",
    )
    threshold_group = parser.add_argument_group(
        "thresholds", "thresholds for scaling attention / score values"
    )
    threshold_group.add_argument(
        "--mask-threshold",
        metavar="THRESH",
        type=int,
        default=20,
        help="Brightness threshold for background removal.",
    )
    threshold_group.add_argument(
        "--att-upper-threshold",
        metavar="THRESH",
        type=float,
        default=1.0,
        help="Quantile to squash attention from during attention scaling "
        " (e.g. 0.99 will lead to the top 1%% of attention scores"
        " becoming 1)",
    )
    threshold_group.add_argument(
        "--att-lower-threshold",
        metavar="THRESH",
        type=float,
        default=0.01,
        help="Quantile to squash attention to during attention scaling "
        " (e.g. 0.01 will lead to the bottom 1%% of attention scores"
        " becoming 0)",
    )
    threshold_group.add_argument(
        "--score-threshold",
        metavar="THRESH",
        type=float,
        default=0.95,
        help="Quantile to consider in score scaling "
        "(e.g. 0.95 will discard the top / bottom 5%% of score values"
        " as outliers)",
    )
    colormap_group = parser.add_argument_group(
        "colors",
        "color maps to use for attention / score maps"
        " (see https://matplotlib.org/stable/tutorials/colors/colormaps.html)",
    )
    colormap_group.add_argument(
        "--att-cmap",
        metavar="CMAP",
        type=str,
        default="magma",
        help="Color map to use for the attention heatmap.",
    )
    colormap_group.add_argument(
        "--score-cmap",
        metavar="CMAP",
        type=str,
        default="coolwarm",
        help="Color map to use for the score heatmap.",
    )
    colormap_group.add_argument(
        "--att-alpha",
        metavar="ALPHA",
        type=float,
        default=0.5,
        help="Opaqueness of attention map.",
    )
    colormap_group.add_argument(
        "--score-alpha",
        metavar="ALPHA",
        type=float,
        default=1.0,
        help="Opaqueness of score map at highest-attention location.",
    )
    args = parser.parse_args()
    if not args.cache_dir:
        warnings.warn(
            "no cache directory specified!"
            " If you are generating heat maps for multiple targets, "
            "it is HIGHLY recommended to manually set a cache directory."
            " This directory should be the SAME for each run."
        )
        args.cache_dir = args.output_path / "cache"

    assert (
        args.att_upper_threshold >= 0 and args.att_upper_threshold <= 1
    ), "threshold needs to be between 0 and 1."
    assert (
        args.att_lower_threshold >= 0 and args.att_lower_threshold <= 1
    ), "threshold needs to be between 0 and 1."
    assert (
        args.att_lower_threshold < args.att_upper_threshold
    ), "lower attention threshold needs to be lower" \
        " than upper attention threshold."

# load base fully convolutional model (w/o pooling / flattening or head)
# In this case we're loading the xiyue wang RetCLL model,
# change this bit for other networks
if (p := "./RetCCL") not in sys.path:
    sys.path = [p] + sys.path
import ResNet

import torch.nn as nn
import torch
from torchvision import transforms
import os
from matplotlib import pyplot as plt
# import openslide
from tqdm import tqdm
import numpy as np
from fastai.vision.all import load_learner
from pyzstd import ZstdFile
import PIL
from sftp import get_wsi

# APC data
# from skimage.filters import gaussian
# from skimage.color import rgba2rgb
from skimage.io import imread
from skimage.io import imsave
from skimage.transform import resize

# supress DecompressionBombWarning: yes, our files are really that big (‘-’*)
# PIL.Image.MAX_IMAGE_PIXELS = None


def _load_tile(
    # slide: openslide.OpenSlide,
    slide,  # numpy array from skimage
    pos: Tuple[int, int],
    stride: Tuple[int, int],
    target_size  # : Tuple[int, int], # Now np.array
) -> np.ndarray:
    # Loads part of a WSI. Used for parallelization with ThreadPoolExecutor
    # tile = slide.read_region(
    #   pos, 0, stride).convert("RGB").resize(target_size)
    tile = slide[pos[0]:pos[0] + stride[0], pos[1]:pos[1] + stride[1]]
    tile = resize(tile, tuple(target_size), preserve_range=True)
    tile = np.repeat(tile[:, :, np.newaxis], 3, axis=2)
    # return np.array(tile)
    return tile


# def load_slide(slide: openslide.OpenSlide,
#                target_mpp: float = 256 / 224
#                ) -> np.ndarray:
def load_slide(slide,
               target_mpp: float = 256 / 224
               ) -> np.ndarray:  # slide is now array from skimage
    """Loads a slide into a numpy array."""
    # We load the slides in tiles to
    #  1. parallelize the loading process
    #  2. not use too much data when then scaling down the tiles from their
    #     initial size
    steps = 8
    # stride = np.ceil(np.array(slide.dimensions) / steps).astype(int)
    stride = np.ceil(np.asarray(slide.shape) / steps).astype(int)
    # slide_mpp = float(slide.properties[openslide.PROPERTY_NAME_MPP_X])
    slide_mpp = 0.1  # HARDCODE HERE FOR NOW
    tile_target_size = np.round(stride * slide_mpp / target_mpp).astype(int)

    with futures.ThreadPoolExecutor(min(32, os.cpu_count() or 1)) as executor:
        # map from future to its (row, col) index
        future_coords: Dict[futures.Future, Tuple[int, int]] = {}
        for i in range(steps):  # row
            for j in range(steps):  # column
                future = executor.submit(
                    # _load_tile, slide, (stride * (j, i)),
                    # stride, tile_target_size  # type: ignore
                    _load_tile,
                    slide,
                    (stride * (i, j)),
                    stride,
                    tile_target_size  # type: ignore
                    )
                future_coords[future] = (i, j)

        # write the loaded tiles into an image as soon as they are loaded
        # im = np.zeros((*(tile_target_size * steps)[::-1], 3), dtype=np.uint8)
        im = np.zeros((*(tile_target_size * steps), 3), dtype=np.uint8)

        for tile_future in tqdm(
            futures.as_completed(future_coords),
            total=steps * steps,
            desc="Loading WSI",
            leave=False,
        ):
            i, j = future_coords[tile_future]
            tile = tile_future.result()

            x, y = tile_target_size * (i, j)  # * (j, i)
            # im[y : y + tile.shape[0], x : x + tile.shape[1], :] = tile
            im[x : x + tile.shape[0], y : y + tile.shape[1], :] = tile

    return im


def batch1d_to_batch_2d(batch1d):
    batch2d = nn.BatchNorm2d(batch1d.num_features)
    batch2d.state_dict = batch1d.state_dict
    return batch2d


def dropout1d_to_dropout2d(dropout1d):
    return nn.Dropout2d(dropout1d.p)


def linear_to_conv2d(linear):
    """Converts a fully connected layer to a 1x1 Conv2d layer
    with the same weights.
    """
    conv = nn.Conv2d(in_channels=linear.in_features,
                     out_channels=linear.out_features,
                     kernel_size=1
                     )
    conv.load_state_dict(
        {
            "weight": linear.weight.view(conv.weight.shape),
            "bias": linear.bias.view(conv.bias.shape),
        }
    )
    return conv


if __name__ == "__main__":
    # use all the threads
    torch.set_num_threads(os.cpu_count() or 1)
    torch.set_num_interop_threads(os.cpu_count() or 1)

    if args.force_cpu:
        device = torch.device("cpu")
    else:
        device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    # default imgnet transforms
    tfms = transforms.Compose(
        [
            transforms.ToTensor(),
            transforms.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )

    base_model = ResNet.resnet50(
        num_classes=128, mlp=False, two_branch=False, normlinear=True
    )
    pretext_model = torch.load("./xiyue-wang.pth", map_location=device)
    base_model.avgpool = nn.Identity()
    base_model.flatten = nn.Identity()
    base_model.fc = nn.Identity()
    base_model.load_state_dict(pretext_model, strict=True)
    base_model = base_model.eval().to(device)

    # transform MIL model into fully convolutional equivalent
    learn = load_learner(args.model_path)
    classes = learn.dls.train.dataset._datasets[-1].encode.categories_[0]
    assert args.true_class in classes, (
        f"{args.true_class} not a target of {args.model_path}! "
        f"(Did you mean any of {list(classes)}?)"
    )
    true_class_idx = (classes == args.true_class).argmax()
    att = (
        nn.Sequential(
            linear_to_conv2d(learn.encoder[0]),
            nn.ReLU(),
            linear_to_conv2d(learn.attention[0]),
            nn.Tanh(),
            linear_to_conv2d(learn.attention[2]),
        )
        .eval()
        .to(device)
    )

    score = (
        nn.Sequential(
            linear_to_conv2d(learn.encoder[0]),
            nn.ReLU(),
            batch1d_to_batch_2d(learn.head[1]),
            dropout1d_to_dropout2d(learn.head[2]),
            linear_to_conv2d(learn.head[3]),
        )
        .eval()
        .to(device)
    )

    # we operate in two steps: we first collect all attention values / scores,
    # the entirety of which we then calculate our scaling parameters from.
    # Only then we output the actual maps.
    attention_maps: Dict[str, torch.Tensor] = {}
    score_maps: Dict[str, torch.Tensor] = {}
    masks: Dict[str, torch.Tensor] = {}

    print("Extracting features, attentions and scores...")
    for slide_url in (progress := tqdm(args.slide_urls, leave=False)):
        slide_name = Path(slide_url.path).stem
        progress.set_description(slide_name)
        slide_cache_dir = args.cache_dir / slide_name
        slide_cache_dir.mkdir(parents=True, exist_ok=True)

        # Load FOV image if there is one in cache,
        # or make one from the specified input, with scaling for visualisation
        if len(sorted(slide_cache_dir.glob('fov.tif'))) > 0:
            # if (fov_tif := slide_cache_dir / "fov*.tif").exists():
            # slide_array = np.array(PIL.Image.open(slide_jpg))
            # print('Using cache')
            fov_tif_path = sorted(slide_cache_dir.glob('fov.tif'))[0]
            slide_array = imread(fov_tif_path)
            if len(sorted(slide_cache_dir.glob('fov.tif'))) > 1:
                print('Warning: There was more than one fov image '
                      'for input in cache.'
                      )
                print('Selected input image: {}'.format(fov_tif_path))

        else:
            # print('Not using cache')
            # WHAT DOES THIS DO?
            slide_path = get_wsi(slide_url, cache_dir=args.cache_dir)
            # slide = openslide.OpenSlide(str(slide_path))
            slide = imread(slide_path)
            # slide_array = load_slide(slide)

            # From grey to 3-channel
            slide_array = np.repeat(slide[:, :, np.newaxis], 3, axis=2)
            # PIL.Image.fromarray(slide_array).save(slide_jpg)

            imsave(slide_cache_dir / 'fov.tif',
                   slide_array,
                   check_contrast=False
                   )

        # pass the WSI through the fully convolutional network'
        # since our RAM is still too small, we do this in two steps
        # (if you run out of RAM, try upping the number of slices)
        if (feats_pt := slide_cache_dir / "feats.pt.zst").exists():
            with ZstdFile(feats_pt, mode="rb") as fp:
                feat_t = torch.load(io.BytesIO(fp.read()))
            feat_t = feat_t.float()
        elif (slide_cache_dir / "feats.pt").exists():
            feat_t = torch.load(slide_cache_dir / "feats.pt").float()
        else:
            max_slice_size = 0xA800000  # experimentally determined
            # ceil(pixels/max_slice_size)
            # TRY SETTING NO SLICES
            no_slices = 1
            # no_slices = (
            #    np.prod(slide_array.shape) + max_slice_size - 1
            #    ) // max_slice_size
            step = slide_array.shape[1] // no_slices
            slices = []
            for slice_i in range(no_slices):
                x = tfms(slide_array[
                            :, slice_i * step : (slice_i + 1) * step, :
                            ]
                         )
                with torch.inference_mode():
                    res = base_model(x.unsqueeze(0).to(device))
                    slices.append(res.detach().cpu())
            feat_t = torch.concat(slices, 3).squeeze()
            # save the features (with compression)
            with ZstdFile(feats_pt, mode="wb") as fp:
                torch.save(feat_t, fp)  # type: ignore

        feat_t = feat_t.to(device)
        # pool features, but use gaussian blur instead of avg pooling
        # to reduce artifacts
        if args.blur_kernel_size:
            feat_t = transforms.functional.gaussian_blur(
                feat_t, kernel_size=args.blur_kernel_size
            )

        # calculate attention / classification scores
        # according to the MIL model
        with torch.inference_mode():
            att_map = att(feat_t).squeeze().cpu()
            score_map = score(feat_t.unsqueeze(0)).squeeze()
            score_map = torch.softmax(score_map, 0).cpu()

        # compute foreground mask
        # Leave some tiles from edges as False,
        # IDEALLY FROM POOLING ARUGUMENT...
        num_tiles_at_edge = 4
        mask = np.full(att_map.shape, False)
        for row in range(num_tiles_at_edge,
                         slide_array.shape[0] // 32
                         - num_tiles_at_edge
                         ):
            for column in range(num_tiles_at_edge,
                                slide_array.shape[1] // 32
                                - num_tiles_at_edge
                                ):
                # Sum over 224 x 224 for mask threshold
                tile = slide_array[:, :, 0][
                    (row - 3) * 32:(row + 4) * 32,
                    (column - 3) * 32:(column + 4) * 32
                    ]
                mask[row, column] = tile.sum() > args.mask_threshold

        attention_maps[slide_name] = att_map
        score_maps[slide_name] = score_map
        masks[slide_name] = mask

    # now we can use all of the features to calculate the scaling factors
    all_attentions = torch.cat(
        [attention_maps[s].view(-1)[masks[s].reshape(-1)]
            for s in score_maps.keys()]
        # Without mask:
        # [attention_maps[s].view(-1) for s in score_maps.keys()]
    )
    att_lower = all_attentions.quantile(args.att_lower_threshold)
    att_upper = all_attentions.quantile(args.att_upper_threshold)

    all_true_scores = torch.cat(
        [
            # mask out background scores, then linearize them
            score_maps[s][true_class_idx].view(-1)[masks[s].reshape(-1)]
            # Without masks:
            # score_maps[s][true_class_idx].view(-1)
            for s in score_maps.keys()
        ]
    )

    # THIS SOMETIMES SEEMS UNHELPFUL AT THE MOMENT -
    # MOST TRUE SCORES ARE ONE SIDE OF 0.5
    centered_score = all_true_scores - (1 / len(classes))
    scale_factor = torch.quantile(centered_score.abs(),
                                  args.score_threshold
                                  ) * 2

    # For scaling cmap
    min_true_score = all_true_scores.min()
    max_true_score = all_true_scores.max()
    mean_true_score = all_true_scores.mean()
    std_true_score = all_true_scores.std()
#    midrange_true_score = (min_true_score + max_true_score) / 2
#    half_range_cmap = \
#        max(abs(min_true_score - 0.5) - 0.5, abs(max_true_score) - 0.5)

    print('\nMin true score: {:.2f}'.format(min_true_score))
    print('\nMax true score: {:.2f}'.format(max_true_score))

    print("Writing heatmaps...")
    for slide_url in (progress := tqdm(args.slide_urls, leave=False)):
        slide_name = Path(slide_url.path).stem
        slide_cache_dir = args.cache_dir / slide_name
        slide_outdir = args.output_path / slide_name
        slide_outdir.mkdir(parents=True, exist_ok=True)

        progress.set_description(slide_name)
        slide_outdir = args.output_path / slide_name

        # slide_im = PIL.Image.open(slide_cache_dir / "slide.jpg")
        slide_im = imread(slide_cache_dir / 'fov.tif')

# ?        if not (slide_outdir / fov_tif_path.name).exists():
# ?          shutil.copyfile(fov_tif_path,
# ?                          slide_outdir / fov_tif_path.name
# ?                          )

        # Make and save saturated image for visualisation
        fraction_nonzeros_to_saturate = 0.2
        # Find brightness value of pixel to scale to 255
        level_to_saturate = np.percentile(
            slide_im[slide_im > 0],
            100. * (1. - fraction_nonzeros_to_saturate)
            )
        # Scale and clip
        slide_im_vis = slide_im * 255. / level_to_saturate
        slide_im_vis[slide_im_vis > 255.] = 255.
        slide_im_vis = np.uint8(np.round(slide_im_vis))
        # Save
        im_vis_save_path = slide_outdir / \
            "fov-sat{}pc.tif".format(
                round(fraction_nonzeros_to_saturate * 100)
                )
        imsave(im_vis_save_path, slide_im_vis, check_contrast=False)

        # Get mask from masks calculated earlier
        mask = masks[slide_name]

        # attention map
        att_map = (attention_maps[slide_name] - att_lower) \
            / (att_upper - att_lower)
        att_map = att_map * mask
        att_map = att_map.clamp(0, 1)

        # bare attention
        im = plt.get_cmap(args.att_cmap)(att_map)
        im[:, :, 3] = mask

        # PIL.Image.fromarray(np.uint8(im * 255.0))\
        # .save(slide_outdir / "attention.png")
        imsave(slide_outdir / 'attention.png',
               np.uint8(np.round(im * 255.0)),
               check_contrast=False
               )

        # attention map (blended with slide)

        # map_im = PIL.Image.fromarray(np.uint8(im * 255.0))

        # Resize to match input image: * 32 for ResNet50
        # and crop right- and bottom-most pixels
        # map_im = map_im.resize(slide_im.size, PIL.Image.Resampling.NEAREST)
        upscaled_att_map = resize(im, [im.shape[0] * 32,
                                       im.shape[1] * 32,
                                       4
                                       ], order=0, preserve_range=True
                                  )
        upscaled_att_map = upscaled_att_map[0:slide_im.shape[0],
                                            0:slide_im.shape[1]
                                            ]
        upscaled_att_map = np.uint8(np.round(upscaled_att_map * 255.))

        imsave(slide_outdir / 'upscaled_attention.png',
               upscaled_att_map,
               check_contrast=False
               )

        att_map_overlay = PIL.Image.fromarray(slide_im_vis, mode='RGB')
        att_map_overlay.convert('RGBA')
        upscaled_att_map[:, :, 3] = np.uint8(np.round(args.att_alpha * 255.))
        upscaled_att_map = PIL.Image.fromarray(upscaled_att_map)
        att_map_overlay.paste(upscaled_att_map, mask=upscaled_att_map)
        att_map_overlay.convert('RGB')
        att_map_overlay.save(slide_outdir / 'attention-map-overlay.png')

        # Multiply FOV image version
#        slide_im_vis_norm = slide_im_vis / 255.  # 0 to 1
#        attention_coded_image = \
#           upscaled_att_map[:, :, 0:3] * slide_im_vis_norm
#        attention_coded_image = np.uint8(attention_coded_image)
#        imsave(slide_outdir / 'attention-coded-fov.tif',
#               attention_coded_image,
#               check_contrast=False
#               )

        # Score map

        # THIS WAS THE ORIGINAL SCALING
#        scaled_score_map = (
#            score_maps[slide_name][true_class_idx] - 1 / len(classes)
#        ) / scale_factor + 1 / len(classes)
#        scaled_score_map = (scaled_score_map * mask).clamp(0, 1)

        # scales to 0-1
#        score_map_min_0 = \
#            score_maps[slide_name][true_class_idx] \
#            - score_maps[slide_name][true_class_idx].min()
#        scaled_score_map = \
#            score_map_min_0 / score_map_min_0.max()

        # ANOTHER SCALING OPTION:
        # 0.5 will be at cmap 0.5; furthest from 0.5 is cmap 0 or 1
        # score_map = score_maps[slide_name][true_class_idx]
        # scaled_score_map = 0.5 + (score_map - 0.5) * 0.5 / half_range_cmap

        # AND ANOTHER:
        # Scales true scores to 0-1
#        score_map = score_maps[slide_name][true_class_idx]
#        scaled_score_map = \
#            0.5 * ((score_map - midrange_true_score)
#                   / (max_true_score - midrange_true_score)
#                   + 1
#                   )

        # AND ANOTHER:
        # Scale mean +- 3 * std to 0-1
        score_map = score_maps[slide_name][true_class_idx]
        scaled_score_map = \
            ((score_map - mean_true_score) / (3 * std_true_score) + 1) * 0.5

        # Include score_threshold argument for scaling
        # scaled_score_map = \
        #   (scaled_score_map - 0.5) * 0.5 / (args.score_threshold - 0.5) + 0.5
        # THRESHOLD ONLY HIGH
        # scaled_score_map = scaled_score_map / args.score_threshold

        scaled_score_map = scaled_score_map.clamp(0, 1)

        # create image with RGB from scores, Alpha from attention
        im = plt.get_cmap(args.score_cmap)(scaled_score_map)
        im[:, :, 3] = att_map * mask * args.score_alpha

        map_im = np.uint8(im * 255.0)

        imsave(slide_outdir / 'score-map.png',
               map_im,
               check_contrast=False
               )

        # Upscaled score map

        # map_im = map_im.resize(slide_im.size, PIL.Image.Resampling.NEAREST)

        # Resize to match input image: * 32 for ResNet50
        # and crop right- and bottom-most pixels
        # map_im = map_im.resize(slide_im.size, PIL.Image.Resampling.NEAREST)
        map_im = resize(im, [im.shape[0] * 32,
                             im.shape[1] * 32,
                             4
                             ], order=0, preserve_range=True
                        )
        map_im = np.uint8(np.round(map_im[0:slide_im.shape[0],
                                          0:slide_im.shape[1]] * 255.
                                   )
                          )
        map_im_save = PIL.Image.new(mode='RGBA',
                                    size=(map_im.shape[1], map_im.shape[0]),
                                    color='white'
                                    )
        map_im = PIL.Image.fromarray(map_im)
        map_im_save.paste(map_im, mask=map_im)
        map_im_save.convert('RGB')

        map_im_save.save(slide_outdir / 'upscaled_score-map.png')

        # Multiply FOV image by score map
#        slide_im_vis_norm = slide_im_vis / 255.  # 0 to 1
#        score_coded_image = map_im[:, :, 0:3] * slide_im_vis_norm
#        score_coded_image = np.uint8(score_coded_image)
#        imsave(slide_outdir / 'score-coded-fov.tif',
#               score_coded_image,
#               check_contrast=False
#               )

        # Overlay scores onto image, transparency is attention score
        score_map_overlay = PIL.Image.fromarray(slide_im_vis, mode='RGB')
        score_map_overlay.convert('RGBA')
        # map_im = PIL.Image.fromarray(map_im)
        score_map_overlay.paste(map_im, mask=map_im)
        score_map_overlay.convert('RGB')
        score_map_overlay.save(slide_outdir / 'score-map-overlay.png')
