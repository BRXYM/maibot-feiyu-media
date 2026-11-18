"""Run a single local SD process per picture, then release the GPU."""
from pathlib import Path

from PIL import Image, ImageOps

import argparse
import asyncio
import secrets
import subprocess
import time
import uuid


BOT = Path(__file__).resolve().parents[2]
INSTALL = BOT.parent / 'FeiyuDiffusion'
OUTPUT = BOT / 'feiyu-tools' / 'diffusion-output'
ENGINE = INSTALL / 'engine' / 'sd-cli.exe'
MODEL = INSTALL / 'awpainting-v12.safetensors'
VAE = INSTALL / 'vae-ft-mse.safetensors'
IP_ADAPTER = INSTALL / 'ip-adapter-plus-sd15.safetensors'
CLIP_VISION = INSTALL / 'clip-vision-h.safetensors'
STICKER_PACK = Path.home() / '.nekowhale-stickers' / 'packs' / '大肥鱼'
DEFAULT_STICKER = '小问号'
NEGATIVE = 'low quality, blurry, malformed hands, extra limbs, text, signature, watermark, nsfw, nudity, gore'
FEIYU_STYLE = ('2D anime key visual, fine hand-drawn ink lines, matte cel shading, '
               'natural local colors, soft window light, controlled highlights')
FEIYU_NEGATIVE = 'oversaturated, neon palette, heavy outlines, glossy plastic, 3d render, airbrushed skin, blue color cast'
FEIYU_LOOK = ('one adult anime woman, long navy hair fading to mist blue, one curved ahoge, soft blue eyes, '
              '(a pair of small sideways whale-fin ears, blue upper surfaces and pale undersides:1.3), '
              'white ruffled headband, navy and white maid dress with a ribbon')
FEIYU_TAIL = ('two human legs, (one blue whale tail attached behind her hips:1.25), '
              'blue upper surface, pale underside, one broad horizontal fluke split into two lobes')
FEIYU_ANATOMY_NEGATIVE = 'visible human ears, pointed elf ears, cat ears, animal ears on top of head, horns, dragon horns'
FEIYU_TAIL_NEGATIVE = 'mermaid body, fish tail replacing legs, vertical fish fin, multiple tails, detached tail'
FEIYU_FRAMING = {
    'expression': 'close-up portrait, face clearly visible, simple background',
    'scene': 'intimate slice-of-life illustration, coherent interior, one clear action',
    'status': 'candid slice-of-life illustration of the character right now, one clear action, natural body language',
    'environment': 'slice-of-life background illustration, quiet point of view, environmental storytelling, empty room, no people',
}


def feiyu_prompt(kind: str, detail: str) -> str:
    parts = [FEIYU_STYLE]
    if kind != 'environment':
        parts.append(FEIYU_LOOK)
    if kind in ('scene', 'status'):
        parts.append(FEIYU_TAIL)
    return ', '.join([*parts, FEIYU_FRAMING[kind], detail])


def resolve_sticker(name: str) -> Path:
    pack = STICKER_PACK.resolve()
    matches = [path for path in pack.glob('*.gif')
               if name.strip() in (path.name, path.stem, path.stem.partition('_')[2])
               and path.resolve().parent == pack and path.is_file()]
    if len(matches) != 1:
        raise ValueError('请使用大肥鱼表情库中的准确名称，例如小问号、害羞、哈欠或读好书')
    return matches[0].resolve()


def prepare_sticker_frame(source: Path, target: Path) -> None:
    with Image.open(source) as animation:
        for index in range(min(animation.n_frames, 24)):
            animation.seek(index)
            frame = animation.convert('RGBA')
            background = Image.new('RGBA', frame.size, 'white')
            rgb = Image.alpha_composite(background, frame).convert('RGB')
            reduced = ImageOps.grayscale(rgb.resize((64, 64)))
            if sum(reduced.histogram()[:230]) >= 410:
                rgb.save(target, 'PNG')
                return
    raise ValueError('这个表情开头没有清楚的参考画面，请换一张')


def _check_ready(reference: Path | None):
    files = (ENGINE, MODEL, VAE, IP_ADAPTER, CLIP_VISION) if reference else (ENGINE, MODEL, VAE)
    if not all(path.is_file() for path in files):
        raise RuntimeError('本地绘图程序或模型尚未安装完成')


async def generate_image(prompt: str, seed: int, kind: str | None = None, reference: Path | None = None) -> Path:
    await asyncio.to_thread(_check_ready, reference)
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection('127.0.0.1', 9881), 5)
    except ConnectionRefusedError:
        pass
    else:
        writer.close()
        await writer.wait_closed()
        raise RuntimeError('本地语音模型服务正在运行，请先暂停它再画图')

    job = OUTPUT / uuid.uuid4().hex
    await asyncio.to_thread(job.mkdir, parents=True)
    image = job / 'image.png'
    log = job / 'engine.log'
    width, height = ('768', '512') if kind == 'environment' else ('512', '768')
    if kind:
        prompt = feiyu_prompt(kind, prompt)
    negative = NEGATIVE + ', ' + FEIYU_NEGATIVE if kind else NEGATIVE
    if kind and kind != 'environment':
        negative += ', ' + FEIYU_ANATOMY_NEGATIVE
    if kind in ('scene', 'status'):
        negative += ', ' + FEIYU_TAIL_NEGATIVE
    args = [str(ENGINE), '-m', str(MODEL), '--vae', str(VAE), '--clip-skip', '2',
            '--mmap', '--backend', 'vulkan0', '--max-vram', '4',
            '--vae-tiling', '--width', width, '--height', height, '--steps', '24',
            '--sampling-method', 'dpm++2m', '--seed', str(seed),
            '--prompt', 'illustration, ' + prompt, '--negative-prompt', negative, '-o', str(image)]
    if kind:
        args += ['--cfg-scale', '5.8', '--scheduler', 'karras']
    if reference:
        reference_frame = job / 'reference.png'
        await asyncio.to_thread(prepare_sticker_frame, reference, reference_frame)
        args += ['--clip_vision', str(CLIP_VISION), '--ip-adapter', str(IP_ADAPTER),
                 '--ip-adapter-image', str(reference_frame), '--ip-adapter-strength',
                 '0.42' if kind == 'expression' else '0.27']
    with log.open('wb') as output:
        process = await asyncio.create_subprocess_exec(
            *args, cwd=ENGINE.parent, stdin=subprocess.DEVNULL,
            stdout=output, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            code = await asyncio.wait_for(process.wait(), 120)
        except (TimeoutError, asyncio.CancelledError):
            if process.returncode is None:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
            raise
    if code or not await asyncio.to_thread(image.is_file):
        raise RuntimeError(f'本地绘图失败（退出码 {code}），详见 {log}')
    return image


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('prompt')
    parser.add_argument('--seed', type=int, default=secrets.randbelow(2**31))
    parser.add_argument('--kind', choices=FEIYU_FRAMING)
    parser.add_argument('--reference', help='Exact sticker name from the local 大肥鱼 pack')
    options = parser.parse_args()
    start = time.monotonic()
    reference = resolve_sticker(options.reference) if options.reference else None
    print(asyncio.run(generate_image(options.prompt, options.seed, options.kind, reference)))
    print(f'{time.monotonic() - start:.1f}s, seed={options.seed}')
