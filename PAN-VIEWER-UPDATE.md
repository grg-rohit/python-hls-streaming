# Quality Comparison — draggable 100% detail

The earlier 100% viewer only used a 512×512 center crop, so it could not reveal other image areas. This release adds **temporary pan previews** based on the actual benchmark outputs, using native pixels for outputs up to 3840 pixels wide. For larger outputs, pan previews are bounded to 3840 pixels and the UI explicitly states the limit.

In the Performance Lab quality-comparison results, select **100% detail** and drag inside the image using a mouse or touch. Baseline and candidate move in lockstep; the Reveal candidate slider stays independent. Switch to **Full frame** to reset the view.

The previews are produced only for new benchmark runs. Existing benchmark results without pan previews retain the original fixed crop. The new full-frame JPEGs are included in transient benchmark responses (up to 3.5 MB per output); consider persistent object storage with expiring signed URLs before deploying with numerous users or replicas. A backend using in-memory benchmark state remains single-replica only. No production video is changed.
