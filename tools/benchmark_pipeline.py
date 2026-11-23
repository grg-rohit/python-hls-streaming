#!/usr/bin/env python3
"""Read-only-to-production benchmark of the existing HLS and FrameLift pipelines.

Never modifies app config or existing streams. Outputs are isolated in a temporary
folder and deleted unless --keep-outputs is set.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app import ffmpeg_service as hls  # noqa: E402


def probe(path):
    result = subprocess.run([hls._ffprobe_binary(), '-v', 'error', '-show_entries',
                             'format=duration:stream=codec_name,width,height,avg_frame_rate',
                             '-select_streams', 'v:0', '-of', 'json', str(path)],
                            capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def run(cmd):
    start = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    return time.perf_counter() - start, result


def parse_metric(stderr, metric):
    pattern = r'All:\s*([\d.]+)' if metric == 'ssim' else r'average:\s*([\d.]+)'
    matches = re.findall(pattern, stderr)
    return float(matches[-1]) if matches else None


def compare_to_source(source, playlist, quality, seconds):
    """Compare decoded HLS to source resized identically; not an AI quality metric."""
    w, h = quality['width'], quality['height']
    vf = (f'[0:v]scale={w}:{h}:force_original_aspect_ratio=decrease:'
          f'force_divisible_by=2,pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,format=yuv420p[ref];'
          '[1:v]format=yuv420p[encoded];[ref][encoded]ssim=shortest=1')
    cmd = [hls._ffmpeg_binary(), '-hide_banner', '-nostdin', '-v', 'info',
           '-t', str(seconds), '-i', str(source), '-i', str(playlist),
           '-filter_complex', vf, '-an', '-f', 'null', '-']
    _, result = run(cmd)
    return {'ssim_vs_scaled_source': parse_metric(result.stderr, 'ssim') if result.returncode == 0 else None,
            'measurement_error': result.stderr[-550:] if result.returncode != 0 else None}


def encode_case(source, quality, preset, workdir, seconds, mode):
    name = 'case_' + preset + '_' + quality['name']
    slug = name
    trimmed = workdir / 'sample.mp4'
    old_streams = hls.STREAMS_DIR
    hls.STREAMS_DIR = workdir / 'streams'
    try:
        cmd = hls._build_cmd(hls._ffmpeg_binary(), quality, trimmed, slug, False, None)
        if mode != 'cpu':
            raise ValueError('Only CPU presets are compared in this benchmark; benchmark GPU separately on the AWS worker')
        i = cmd.index('-preset')
        cmd[i + 1] = preset
        elapsed, result = run(cmd)
        if result.returncode != 0:
            raise RuntimeError(result.stderr[-1500:])
        outputs = hls.stream_dir(slug) / quality['name']
        playlist = outputs / 'playlist.m3u8'
        if not playlist.is_file():
            raise RuntimeError('FFmpeg did not generate HLS playlist')
        size = sum(p.stat().st_size for p in outputs.rglob('*') if p.is_file())
        metrics = compare_to_source(trimmed, playlist, quality, seconds)
        return {'type': 'hls', 'quality': quality['name'], 'preset': preset,
                'elapsed_seconds': round(elapsed, 3), 'output_bytes': size,
                'processing_fps_relative_to_video': round(seconds / elapsed, 3) if elapsed else None,
                **metrics}
    finally:
        hls.STREAMS_DIR = old_streams


def benchmark_ai(sample, workdir, args):
    from app.ai_upscaler import upscale_video
    dest = workdir / 'ai_enhanced.mp4'
    start = time.perf_counter()
    upscale_video(sample, dest, scale=args.ai_scale, model=args.ai_model,
                  device=args.ai_device, tile=args.ai_tile,
                  encoder=args.ai_encoder)
    elapsed = time.perf_counter() - start
    return {'type': 'ai', 'model': args.ai_model, 'scale': args.ai_scale,
            'device': args.ai_device, 'tile': args.ai_tile,
            'elapsed_seconds': round(elapsed, 3), 'output_bytes': dest.stat().st_size,
            'output_probe': probe(dest)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path, help='Local input video (never modified)')
    parser.add_argument('--seconds', type=float, default=10, help='Short sample duration (default 10)')
    parser.add_argument('--quality', default='480p', choices=list(hls.QUALITY_BY_NAME))
    parser.add_argument('--presets', nargs='+', default=['slow', 'medium'],
                        choices=['veryslow', 'slower', 'slow', 'medium', 'fast', 'faster'])
    parser.add_argument('--ai', action='store_true', help='Also run FrameLift; requires installed model and hardware')
    parser.add_argument('--ai-model', default='realesr-animevideov3')
    parser.add_argument('--ai-scale', type=int, choices=(2, 3, 4), default=2)
    parser.add_argument('--ai-device', choices=('cpu', 'cuda', 'auto'), default='auto')
    parser.add_argument('--ai-tile', type=int, default=384)
    parser.add_argument('--ai-encoder', choices=('cpu', 'nvenc'), default='cpu')
    parser.add_argument('--report', type=Path, default=Path('benchmark-results.json'))
    parser.add_argument('--keep-outputs', action='store_true')
    args = parser.parse_args()
    source = args.source.resolve()
    if not source.is_file() or args.seconds <= 0:
        parser.error('source must be a file and --seconds must be positive')
    workdir = Path(tempfile.mkdtemp(prefix='hls-benchmark-'))
    data = {'timestamp_utc': datetime.now(timezone.utc).isoformat(),
            'source': str(source), 'source_probe': probe(source),
            'sample_seconds_requested': args.seconds, 'results': [], 'errors': []}
    try:
        sample = workdir / 'sample.mp4'
        # Lossless, intra-only trim is unnecessary: create an H264 benchmark input once,
        # using the same sample for all tests. Audio is omitted to focus on video speed.
        duration, result = run([hls._ffmpeg_binary(), '-hide_banner', '-nostdin', '-y', '-v', 'error',
                                '-i', str(source), '-t', str(args.seconds), '-an',
                                '-c:v', 'libx264', '-crf', '0', '-preset', 'ultrafast', str(sample)])
        if result.returncode != 0:
            raise RuntimeError('Could not prepare benchmark sample: ' + result.stderr[-1200:])
        data['sample_preparation_seconds'] = round(duration, 3)
        sample_probe = probe(sample)
        data['sample_probe'] = sample_probe
        duration_actual = float(sample_probe.get('format', {}).get('duration') or args.seconds)
        w, h = hls.probe_source(sample)
        quality = hls.quality_specs_for_source(w, h, [args.quality])[0]
        for preset in args.presets:
            try:
                data['results'].append(encode_case(sample, quality, preset, workdir, duration_actual, 'cpu'))
            except Exception as exc:
                data['errors'].append({'test': preset, 'error': str(exc)})
        if args.ai:
            try:
                data['results'].append(benchmark_ai(sample, workdir, args))
            except Exception as exc:
                data['errors'].append({'test': 'ai', 'error': str(exc)})
        data['note'] = ('SSIM measures HLS fidelity against a conventionally resized source, '
                        'NOT AI perceptual enhancement or temporal consistency. '
                        'Compare identical quality targets with visual inspection before changing defaults.')
        if args.keep_outputs:
            data['outputs_dir'] = str(workdir)
    finally:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(data, indent=2), encoding='utf-8')
        if not args.keep_outputs:
            shutil.rmtree(workdir, ignore_errors=True)
    print(json.dumps({'report': str(args.report.resolve()), 'results': data['results'],
                      'errors': data['errors']}, indent=2))
    return 1 if data['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
