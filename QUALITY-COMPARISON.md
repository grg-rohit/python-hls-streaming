# AI Quality Comparison (browser)

This release adds **Compare AI models + visual quality** to Performance Lab. Select a video, model A, model B, scale (use the same target resolution), two-second duration and your GPU worker. Two or four isolated AI enhancement runs are made depending on AB/ABBA selection. No publishing or production defaults are modified.

For tile-only tests select **Compare tile sizes**. Both modes return SSIM (downscaled to 640px) and genuine output frames from the final run of each configuration. The interface provides an interactive reveal slider with a 960px overview and native-resolution 512px center crop. Preview images are encoded into the authenticated benchmark status response and held only in API process memory; they are not uploaded to S3. Reboot clears them. Results are viewable to anyone possessing the benchmark access token.

The JPEG previews and the single center crop are only for visual inspection. They are **not** reliable ground-truth AI quality metrics, and they do not measure temporal flicker. SSIM between different AI models indicates similarity, not which is better. Check full videos for temporal consistency before choosing a production default.

GPU tests were not executed in this build environment. Run through your existing CUDA worker and validate before production.
