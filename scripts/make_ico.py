"""Convert logo_icon.png or logo.png to logo.ico for Windows builds."""
import sys, os
from PIL import Image

sizes = [(256,256),(128,128),(64,64),(48,48),(32,32),(24,24),(16,16)]
sources = ['src/logo_icon.png', 'src/logo.png']

for src in sources:
    if os.path.exists(src):
        img = Image.open(src).convert('RGBA')
        resized = [img.resize(s, Image.LANCZOS) for s in sizes]
        resized[0].save('src/logo.ico', format='ICO',
                        sizes=sizes, append_images=resized[1:])
        print(f'Created src/logo.ico from {src}')
        sys.exit(0)

print('WARNING: No logo PNG found — exe will use default icon')
sys.exit(0)
