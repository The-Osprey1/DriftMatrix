"""Save source-executed approved/compact comparisons; no panel connection."""
from hashlib import sha256
import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from ocean_preview import OceanPreview
from led_redesign_preview import led_image
from weather.minify_ax import minify

ROOT = Path(__file__).resolve().parent


def export(output: Path):
    output.mkdir(parents=True, exist_ok=True)
    paths = [ROOT / name for name in ('ocean-v3.1.ax', 'ocean-v3.2.ax')]
    runners = [OceanPreview(path.read_text(encoding='utf-8')) for path in paths]
    for runner in runners:
        runner.show()
    font_path = Path('C:/Windows/Fonts/segoeui.ttf')
    font = ImageFont.truetype(str(font_path), 18) if font_path.exists() else ImageFont.load_default()
    frames, hashes = [], []
    for stamp in range(120, 30001, 120):
        pixels = [runner.advance(120).pixels for runner in runners]
        assert pixels[0] == pixels[1], f'Source pixels differ at {stamp} ms'
        hashes.append(sha256(bytes(component for color in pixels[0] for component in ((color>>16)&255,(color>>8)&255,color&255))).hexdigest())
        board = Image.new('RGB', (1104, 224), (12,17,27))
        draw = ImageDraw.Draw(board)
        draw.text((20,10), 'Approved 3.1 / exact source pixels', font=font, fill='#ecf1fa')
        draw.text((572,10), 'Compact 3.2 / same pixels', font=font, fill='#a5e6ce')
        for x, image in zip((20,572), pixels):
            board.paste(led_image([f'{color:06x}' for color in image]), (x,45))
        draw.text((20,185), f'Offline subset model / {stamp/1000:.2f}s / memory and physical fit are separate', font=font, fill='#aab8ca')
        frames.append(board)
    frames[166].save(output / 'comparison.png')
    frames[0].save(output / 'comparison.gif', save_all=True, append_images=frames[1:], duration=120, loop=0, disposal=1)
    result = {'source_executed': True, 'physical_capture': False, 'all_frames_pixel_equal': True,
              'frames': len(frames), 'visible_ms': 30000, 'draw_step_ms': 40, 'sample_step_ms': 120,
              'sources': [{'path': path.name, 'minified_bytes': len(minify(path.read_text(encoding='utf-8')).encode()),
                           'minified_sha256': sha256(minify(path.read_text(encoding='utf-8')).encode()).hexdigest()} for path in paths],
              'frame_sha256': hashes}
    (output / 'frame-parity.json').write_text(json.dumps(result, indent=2) + '\n', encoding='utf-8')
    return result


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    result = export(args.output)
    print(json.dumps({key:value for key,value in result.items() if key != 'frame_sha256'}, indent=2))
