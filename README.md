# High-Resolution Heatmaps for Marugoto MIL Models

## Include feature extraction model in directory

`create_heatmaps.py` curently expects to work with the RetCCL feature extractor. See [./RetCCL/README.md](https://github.com/AlistairCurd/smlm-attention-maps/tree/main/RetCCL) 

Download best_ckpt.pth from latest Xiyue Wang: https://drive.google.com/drive/folders/1AhstAFVqtTqxeS9WlBpU41BV08LYFUnL.

Place in this repository's home directory and rename to `xiyue-wang.pth` to match what `create_heatmaps.py` expects.

## Options

```sh
create_heatmaps.py [-h] -m MODEL_PATH -o OUTPUT_PATH -t TRUE_CLASS
                   [--no-pool]
                   [--mask-threshold THRESH]
                   [--att-upper-threshold THRESH]
                   [--att-lower-threshold THRESH]
                   [--score-threshold THRESH]
                   [--att-cmap CMAP]
                   [--score-cmap CMAP]
                   SLIDE [SLIDE ...]
```

Create heatmaps for MIL models.

| Positional Arguments | Description |
|----------------------|-------------|
| `SLIDE` | Slides to create heatmaps for.  If multiple slides are given, the normalization of the attention / score maps' intensities will be performed across all slides. |

| Options | Description |
|---------|-------------|
| `-m MODEL_PATH`, `--model-path MODEL_PATH` | MIL model used to generate attention / score maps. |
| `-o OUTPUT_PATH`, `--output-path OUTPUT_PATH` | Path to save results to. |
| `-t TRUE_CLASS`, `--true-class TRUE_CLASS` | Class to be rendered as "hot" in the heatmap. |
| `--no-pool` | Do not average pool features after feature extraction phase. |
| `--cache-dir CACHE_DIR` | Directory to cache extracted features etc. in. |

| Thresholds | Description |
|------------|-------------|
| `--mask-threshold THRESH` | Brightness threshold for background removal. |
| `--att-upper-threshold THRESH` | Quantile to squash attention from during attention scaling (e.g. 0.99 will lead to the top 1% of attention scores to become 1) |
| `--att-lower-threshold THRESH` | Quantile to squash attention to during attention scaling (e.g. 0.01 will lead to the bottom 1% of attention scores to become 0) |
| `--score-threshold THRESH` | Quantile to consider in score scaling (e.g. 0.95 will discard the top / bottom 5% of score values as outliers) |

| Colors | Description |
|--------|-------------|
| `--att-cmap CMAP` | Color map to use for the attention heatmap. |
| `--score-cmap CMAP` | Color map to use for the score heatmap. |

## Running in a Container

The heatmap script can be conveniently run in a podman container.  To do so, use
the `heatmaps-container.sh` convenience script.

```sh
./heatmaps-container.sh \
    -t TRUE_CLASS \
    /wsis/slide1.svs \
    /wsis/slide2.svs
```

In order to use GPU acceleration, the `nvidia-container-toolkit` has to be
installed beforehand.

