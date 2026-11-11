"""Download the official default Minecraft skin/cape textures used by the harness.

The launcher expects 64x64 PNG skin textures and a 64x32 PNG cape texture;
JPEGs are not valid Minecraft skin files. The profile UUIDs below resolve via
Mojang's session server to the current texture URLs, then the images are saved
under harness/assets. Small face crops are also regenerated for account icons.

usage: python re-tools/make_default_skins.py
"""
import base64
import json
import os
import urllib.request
from io import BytesIO

from PIL import Image

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(BASE, "harness", "assets", "skins")
ASSETS = os.path.dirname(OUT)
USER_AGENT = "MinecraftLauncher/3.41.29"

# Official profile identities: Steve, Alex, and Jeb_ (wearing the Mojang cape).
PROFILES = {
    "steve": ("8667ba71b85a4004af54457a9734eed7", "SKIN", (64, 64)),
    "alex": ("ec561538f3fd461daff5086b22154bce", "SKIN", (64, 64)),
    "mojang_cape": ("853c80ef3c3749fdaa49938b674adae6", "CAPE", (64, 32)),
}


def fetch(url):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=20) as response:
        return response.read()


def official_texture(profile_id, texture_kind):
    profile = json.loads(fetch(
        "https://sessionserver.mojang.com/session/minecraft/profile/" + profile_id
    ).decode("utf-8"))
    properties = profile.get("properties") or []
    value = next((item.get("value") for item in properties if item.get("name") == "textures"), None)
    if not value:
        raise RuntimeError("Mojang profile has no textures property: " + profile_id)
    textures = json.loads(base64.b64decode(value))["textures"]
    texture = textures.get(texture_kind)
    if not texture or not texture.get("url"):
        raise RuntimeError("Mojang profile has no " + texture_kind + " texture: " + profile_id)
    return fetch(texture["url"])


def save_texture(filename, content, expected_size):
    image = Image.open(BytesIO(content)).convert("RGBA")
    if image.size != expected_size:
        raise ValueError("%s must be %dx%d, got %dx%d" % (
            filename, expected_size[0], expected_size[1], image.width, image.height
        ))
    image.save(filename, format="PNG")
    return image


def main():
    os.makedirs(OUT, exist_ok=True)
    for name in ("steve", "alex"):
        profile_id, kind, expected_size = PROFILES[name]
        image = save_texture(
            os.path.join(OUT, name + ".png"),
            official_texture(profile_id, kind),
            expected_size,
        )
        image.crop((8, 8, 16, 16)).resize((64, 64), Image.NEAREST).save(
            os.path.join(OUT, name + "_avatar.png"), format="PNG"
        )
        print("wrote official", name, "skin and avatar")

    profile_id, kind, expected_size = PROFILES["mojang_cape"]
    save_texture(
        os.path.join(ASSETS, "mojang_cape.png"),
        official_texture(profile_id, kind),
        expected_size,
    )
    print("wrote official Mojang cape")


if __name__ == "__main__":
    main()
