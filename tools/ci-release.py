"""Build from a pinned public seed; verify bytes before an optional draft publication.

The seed EXE is read as an archive and is never executed. The only publication
credential is the job's ephemeral GITHUB_TOKEN, consumed in memory by urllib/gh.
"""
import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import struct
import subprocess
import tempfile
import urllib.error
import urllib.request
from pathlib import Path, PureWindowsPath

REPOSITORY = "abdulmunthakimwork/minecraft-launcher"
REPOSITORY_ID = 1453904618
SEED_URL = ("https://github.com/" + REPOSITORY + "/releases/download/v3.41.30/"
            "Minecraft-Launcher-Setup-3.41.30.exe")
SEED_SHA256 = "0c8f29898f7274659e7e0efb5a53effc688a80f1059c858010a3d2bad864076d"
HOST_SHA256 = "9585dbc65e8bff63d84311cfe31fbe190bd66fffbbf06f30881ba01e399cae07"
APP_FILES = ("main.js", "lifecycle.js", "updater.js", "preload.js", "start.js")
HARNESS_FILES = ("bridge.js", "ui.js", "installation_ui.js")
NEUTRAL_OVERLAY = {"UiSettings": "{}", "UiEvents": "{}", "UiSkins": "{}",
                   "__launcher_settings": {}, "__configuration_settings": {},
                   "__configurations": {}, "__deleted_configurations": [], "__capes": []}
NEUTRAL_SEEDS = {"launcher_profiles.json": {"profiles": {}}, "launcher_settings.json": {},
                 "launcher_product_state_microsoft_store.json": {},
                 "launcher_ui_state_microsoft_store.json": {}, "treatment_tags.json": {}}
MANIFESTS = {"version_manifest_v2.json": "ac37f92737df1a9d7660047136fedd3edebd90bd6e552f53ddc3f222079bc8fa",
             "jre_manifest.json": "c63202ddd28150e3b1959100cc37fe08a293636ed359bc17fc0ee338b9759781"}
# Exact sanitized image bytes from the reviewed v3.41.30 public release.
SANITIZED_PNGS = {
    "minecraft-launcher-re/src/ui/static/media/background/dungeons2-hero.2b763ced.png": "c22b0e8fb82a2ede79ccedade0e41452ca054a6b0f5df81e020e48e82b57dc28",
    "minecraft-launcher-re/src/ui/static/media/background/legends.caa7826a.png": "21b56539e43b82a5b6af947ff4242d9f8783cf25692f748ebfabe241a2939fca",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Bedrock.png": "4dc0a975958b876461ccc49dd576618132c24eaecbc656c88fb5f34e0f2c3636",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Bookshelf.png": "3c48dc9d657c5ad9ab56d9c630cfeca66468f186d15ff0c14d52b82cc1504654",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Brick.png": "a54ed89a5be1f9c397f2749465748925e81bee93501dfea8f5d2cf794050f9c5",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Cake.png": "01c3f8f06b49217084fafedb27d095aa61494e02e9ba4fbfb1723e5711e3ed35",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Carved_Pumpkin.png": "b8e7dcd2e8bbb0bc448c0e4096f6dc662d3507143cdce9c2b3f16d81dc3caa6e",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Chest.png": "295c7059479d17b2ebc4f20c3b78e3ecf10493ee48083130945c6de74c985d2c",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Clay.png": "22d6a629643ee49b75fa2bac6efc4fd0b5a32581fe9c93e4c6fbeeb31b9f1971",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Coal_Ore.png": "4c1d2780a2ddc30271b9ac6e3233de282fbddbcbccb0c569e73878c152b833cb",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Cobblestone.png": "060f731bf841cdbf0bcc6b7ef4cc11c3eafb90ccf319c6895307dac9042ad318",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Crafting_Table.png": "3421ad4248236e1604720d393e5a923a9995fc0849ca9d1c80b5e66e24c3f2dd",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Creeper_Head.png": "f2ccd1cdb1d270ed2145be8d6e581f550633669483f0b0bfbea7de503f5efe02",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Diamond_Ore.png": "559207e13c64895df07f6a38fcb4692487d95557d1c56a6e8d93c5e32ea4ec09",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Emerald_Ore.png": "97920f3285646810be49f2d9306b7acb857e26adc352bfc72e634073dfa2cd09",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Enchanting_Table.png": "81dc4d61fbe50ff04da8b11ff27773c5c9e5769a80ee8cda042d30b3071275e8",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/End_Stone.png": "519e07db57a68b4a5f6ec6f4010c1e6b0c07cda05d2b7822ab137d3e330b2660",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Farmland.png": "00c47b7533bf69da12e9f07b621a57c4a4aee8d07d76d374e694cd57f84acfe5",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Furnace_On.png": "fe5ba4bc76ef81b365443a1b97614b653707131fe6bdf3490c2aa6c8673ebc58",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Glass.png": "eb5e0db9431201e4b41db3341adc1eb9c5e5771e511b2af97bbf7103a3816b4b",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Glowstone.png": "bf71c77f86bc6e09f43f8a7c66e1c2ebdaadfd94f30651762f43f7ef85b2e81e",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Gold_Ore.png": "f16bc8083334758a5007b7d37a6a75d1c7297e38a6b68ca2ba3617483d526988",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Gravel.png": "982019b5fe9ce0f8fe2b04042a86a0dfe80a92553872bc4807192e0e9a861779",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Ice_Packed.png": "ab5044b7ed5a7687fcc8e856564430f4fc827222ebc769b6997a38b9ca060601",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Iron_Ore.png": "a245fedd32e727b688e803b668cb08bbd866e9952485956b13b5172b7129a056",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Lapis_Ore.png": "31ea9bc2c9f82a16f3e2e1166a2ed993f3271843cb65795c06060914dc121ed0",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Lectern_Book.png": "805d4f357a47602c4c57299d515cb6cd3211d8dd328881950f2933c2c754376c",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Mycelium.png": "38cac134061705ebedb20167dad9406fbf3286ff0e68d573e32e26c7413097c8",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Netherrack.png": "9aae3a4099a5ba05adf39b89893d9a2ab44702b447d1058121ce8a6b0eb7f6b3",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Nether_Brick.png": "0a59ca66313236c3f5f7d90aa8b64b4e0d86419661c435701b5c4a7b71004349",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Obsidian.png": "efed1297be0168c246a4d17153903563a9683eb7a7f2a3fffb5af839c3e378e8",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Redstone_Ore.png": "50070ca1a20fa089ee9994437a77b5737a725c3bd85d28d74ca7bdae822579e6",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Red_Sand.png": "8bc38ea718ad14ff1cac7d5ecb3a2ebbdcf9f64c8c92d2a197088ccb5bf6cbde",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Red_Sandstone.png": "3e44e2ea2f861491df40240a1813334af867359c6ec0c37b24af84a23c075a32",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Sand.png": "1fc14a3608874aa60a1e8c1de819edb9dc7424b8078f0ca652c99b0fc1333bec",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Sandstone.png": "0a6ad85ddca1c01503f909992b19966ab7cb12de957451a333f6755a9c8cc779",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Skeleton_Skull.png": "c98713640321d54db4bd034d3b12d20add50cad5fd79890039b8dbbc2c822044",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Snow.png": "aa380455d1083e440a71114f6f66fe35c76cf87be995adb9d6c9d47d9203e1ee",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Soul_Sand.png": "faed6c26a87353874612d8dfee57df722744e852881ec05c298a36b7a1afb0bc",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Stone.png": "db835509391d72dc926f83bc2b402889df04baa121a413b31b7f7f64a6bcb52e",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/TNT.png": "577016016aaa3762ae23ed38a295bf56b9ffee731f257f66e3acf844ae9d13d3",
    "minecraft-launcher-re/src/ui/assets/icons/javaIcons/Water.png": "19731319c4cb57403b6ee89faf22949897cf7265e3692d1d89e31b56f65db865",
    "harness/content/images/1no6CPUX9GPzZ0XhxMhZN3-Unified_launcher_1200x480px_v2__1-1.png": "cae36c30f6beed312a61efd3a97524ab2a9aaef803c51e03cc1bacca09d7f352",
    "harness/content/v2/images/2G5KR8Tpckma0gGVLnCQIRlibrarian.png": "6ee59bea1929e781f245a15597acef27097506ecf250dba34697ec663388c683",
    "harness/content/v2/images/2SBogbDcjGhPldUdwJZ1bxMCDDLC5DepthsBelowLauncherLogo1640x758.png": "2f6fef0ee5b606214c17d1316e00501a9b924ef5bb281d51064b406a11e942f2",
    "harness/content/v2/images/5GbbbQCu7yWdoNulZ493FKHowlingPeaksLogo.png": "7fab879468b1a4f02052fb856c1f55087a94f0f1b0b2597a05859fb6a0668b4f",
    "harness/assets/media/dungeons2carousel/d2_sift_1.87ac88e9.png": "fd2aa82b6a976fded6d3b7ff7c9b6e4cd51db48a1fa211b743edb90addf18faf",
    "harness/assets/media/dungeons2carousel/d2_sift_10.4bf2046a.png": "54304835bceb6c373e6fe9074841ddc7a30417e926d18125b18de00002fb6edf",
    "harness/assets/media/dungeons2carousel/d2_sift_10_thumb.00239d94.png": "47762a8960a01d1d00e76c412404a76bf50633f26a06db552059ea52a57b5b9b",
    "harness/assets/media/dungeons2carousel/d2_sift_11.dd298abf.png": "80377e14b089813355ebd507269b5e30a1fe17f6c1d863489b8c09ac639da992",
    "harness/assets/media/dungeons2carousel/d2_sift_11_thumb.67fb6a2b.png": "e339ca01f93b2274ca7686516cee9cf005ecb8989e935428e119a88600e4ac93",
    "harness/assets/media/dungeons2carousel/d2_sift_1_thumb.1bd22c1d.png": "237c0693ca4a92c13b2c82945c4a80765c8431a5520a505d4f756bbfb52f60b5",
    "harness/assets/media/dungeons2carousel/d2_sift_2.2b5e3602.png": "0572163d8a231eb0445aedac1784790175139418c63234b180f1b14a607a253c",
    "harness/assets/media/dungeons2carousel/d2_sift_2_thumb.47449a78.png": "1b7f3958474f71a12056a98f2348e4bb40c415e4c0af2f4017769b5e6ccff1dc",
    "harness/assets/media/dungeons2carousel/d2_sift_3.25372d85.png": "2f0dec89a8508f35e8692b1f049c730bc1d5261c6fbf7889139ae70f2666b0b7",
    "harness/assets/media/dungeons2carousel/d2_sift_3_thumb.454758ab.png": "113c179edb4ef38d909719b8d71745366749dad99cf8230aeebcfa2af12a8bee",
    "harness/assets/media/dungeons2carousel/d2_sift_4.e3bfe47a.png": "498fa44e38f663baf4b5d187219a39bb8b287c12d5eef43a9e7b03f99c871475",
    "harness/assets/media/dungeons2carousel/d2_sift_4_thumb.428fd8cb.png": "5955c1959a36d3f7f3132b950b6a3f9eda215a5ed44f5e8888d357c4092faba5",
    "harness/assets/media/dungeons2carousel/d2_sift_5.cee6f2df.png": "9708c369eba1883c13aa1b4ef6b60e3f0104fe6b49d935a94e27446affe8166c",
    "harness/assets/media/dungeons2carousel/d2_sift_5_thumb.b9ccb878.png": "1a4365c252780beabba95818f5175ae3bd5cdcc36d10820e619074b2c089073f",
    "harness/assets/media/dungeons2carousel/d2_sift_6.977fccd5.png": "87c90e636884b9f5f8416dc312b8ff9973064e0fb6c4f02da243349c0be69dcf",
    "harness/assets/media/dungeons2carousel/d2_sift_6_thumb.9a842d29.png": "6eb80e9c8c23ea56db5e958888a1d6f92db46e9248e75354a4f283bd90659d19",
    "harness/assets/media/dungeons2carousel/d2_sift_7.7b7c10fd.png": "be8c418a0a8e2d820f3435c40b4eeb266b1a5ca010907af49cffce6e3f667232",
    "harness/assets/media/dungeons2carousel/d2_sift_7_thumb.bd59918e.png": "0ed537e758501f933093303ca0a06d4d6f9c87297e005d78679798271912b330",
    "harness/assets/media/dungeons2carousel/d2_sift_8.0eb90f5f.png": "6135cfa6d213ffaf1840a77721da8a56a297efe6538d1f1495d1da4782d6fadb",
    "harness/assets/media/dungeons2carousel/d2_sift_8_thumb.4de6a027.png": "6ccc77d46c9f8b88f8694009f1434037ee8d6a6afa0d50f0de394529ddc2a855",
    "harness/assets/media/dungeons2carousel/d2_sift_9.cecb6ebd.png": "f669d4b872953425a9f92abdc5883e1d82a539eedfe42b1783c204b279ddff28",
    "harness/assets/media/dungeons2carousel/d2_sift_9_thumb.262e173d.png": "73892c89778abc74952412a1b7924a3ede4399f0f239e11beaca8fe5f8404251",
    "harness/assets/media/dungeons2carousel/dungeons2-hero-thumb.ea360b7e.png": "7dcdd3d5d9cd201e3bca1a15c3145cdff7729a98efbd432e120693c82cc0728e",
    "harness/assets/media/dungeons2carousel/dungeons2_06.fc2f74d0.png": "5965e15cfd4002616f225b5bea1e77ffff94b03cb3edd26a23f0c93ba56fab67",
    "harness/assets/media/dungeons2carousel/dungeons2_07.f04d4ea5.png": "889605c9bfe7d3048fd56a76da0fbc896e456c1f482814cd83877ca11d998ed7",
    "harness/assets/media/dungeons2carousel/dungeons2_08.be6d119b.png": "2b2ea25ac2d8386f6dde8489f6017fa158e5577f1490fda31339aad2ccd0fbcf",
    "harness/assets/media/dungeons2carousel/dungeons2_09.b576b2c8.png": "d77e70ead570983bd053193c3cda6a415e78ad96c6f5bfdb66a79054deb88242",
    "harness/assets/media/images/d2_creativity.5a163999.png": "e29255087f50d948c705fdf3cf86644ab2f6193072e29eef6259d78d8fe2c502",
    "harness/assets/media/images/d2_explore.0177ec76.png": "7d6414d8dbfcbea9c96b0c36ce5ef263f4fbafe2549382a349d55ec4aa9b64f2",
    "harness/assets/media/images/d2_friends.361d5780.png": "24ed200dc7f325f2b888925df6f8493c61bcb12a524707d86558392a428319dd",
    "harness/assets/media/images/d2_skills.87915b1c.png": "cefeadae9dbd3ab11d53dc2639f4f479346f607ed38473efb3039e0808f23940",
    "harness/assets/media/images/dungeons2_trailer_thumb.25ebc1c5.png": "a407e6b83aaacb505b801c88fc1c8a0fc17a5372b4f3ef35bb5856443e4d8ee1",
    "harness/assets/media/images/dungeons2_upsell_promo.573c6b13.png": "4e5e69536e85d584691685a715663101c50e10617b2b2c50b6f6d036e42997be",
    "harness/assets/media/profile/realms_banner.0fbd1bf0.png": "f5a7442141d56311e5873929613f4ff5fc80a27ff1a620ef5c2d61e32a4eb148",
    "harness/assets/media/textures/MCD2_Bespoke_Soul_Small_RGB.bcd3310e.png": "1d2862187f33bf3c207df13767293863e665f2b1e0daa6d09b818bc1b7959dc6",
    "harness/assets/media/textures/particleTexture.2900e9f4.png": "abcc12340f4e9c66c43a104c309d2b2d9f1f362d5d6dc276aa8e183928fce057",
    "harness/assets/media/textures/Soul-Sprite_trail.3ce4d990.png": "b789640393470eef6558377836e1c009bcf3042883c60cda653cb46fd8c67dc2"
}


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def long_path(path):
    path = Path(path).resolve()
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        return Path("\\\\?\\" + str(path))
    return path


def sha(file):
    digest = hashlib.sha256()
    with long_path(file).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def read_json(file):
    return json.loads(long_path(file).read_text(encoding="utf-8"))


def write_json(file, value):
    long_path(file).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def hashes(folder):
    folder = long_path(folder)
    result = {}
    for file in sorted(folder.rglob("*")):
        require(not file.is_symlink(), "Resource symlinks are forbidden")
        if file.is_file():
            result[file.relative_to(folder).as_posix()] = sha(file)
    return result


def tree_sha(values):
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def require_clean_checkout(root, label="Checkout"):
    result = subprocess.run(["git", "diff", "--quiet", "HEAD", "--", "."], cwd=root,
                            capture_output=True, text=True)
    require(result.returncode == 0, label + " tracked files or index differ from its commit")


def require_checkout_commit(root, expected, label):
    require(re.fullmatch(r"[0-9a-f]{40}", expected), label + " requires a full commit SHA")
    commit = run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    require(commit == expected, label + " HEAD differs from its expected commit")
    require_clean_checkout(root, label)


def release_provenance(source=None, workflow=None):
    return {"sourceCommit": source if source is not None else os.environ.get("RELEASE_SOURCE_SHA", "local-dry-run"),
            "workflowCommit": workflow if workflow is not None else os.environ.get("GITHUB_SHA", "local-dry-run")}


def require_report_provenance(report, version, source, workflow):
    require(report["passed"] and report["version"] == version and report["sourceCommit"] == source
            and report["workflowCommit"] == workflow,
            "Verified CI report differs from the release source or workflow commit")


def run(args, **kwargs):
    return subprocess.run([str(a) for a in args], check=True, **kwargs)


def safe_archive_listing(listing):
    for line in listing.splitlines():
        if line.startswith("Path = "):
            path = PureWindowsPath(line[7:])
            require(not path.is_absolute() and not path.drive and not path.root and ".." not in path.parts
                    and ":" not in line[7:], "Unsafe archive path")
        require(not line.startswith(("Symbolic Link = ", "Hard Link = ")),
                "Archive links are forbidden")


def extract_payload(exe, destination, seven):
    """Safely extract the inner archive, then merge external compressed videos."""
    destination = long_path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    outer = run([seven, "l", "-slt", "-ba", exe], capture_output=True, text=True).stdout
    safe_archive_listing(outer)
    archive = destination / "app-64.7z"
    with archive.open("wb") as stream:
        run([seven, "e", "-so", "-bso0", "-bsp0", exe, r"$PLUGINSDIR\app-64.7z"],
            stdout=stream, stderr=subprocess.PIPE)
    require(archive.stat().st_size > 0, "Installer payload archive is missing")
    listing = run([seven, "l", "-slt", "-ba", archive], capture_output=True, text=True).stdout
    safe_archive_listing(listing)
    unpacked = destination / "unpacked"
    run([seven, "x", archive, r"resources\*", "LICENSE.electron.txt", "LICENSES.chromium.html",
         "-o" + str(unpacked), "-y", "-bso0", "-bsp0"], capture_output=True)
    run([seven, "x", exe, r"resources\*", "-o" + str(unpacked), "-y", "-bso0", "-bsp0"],
        capture_output=True)
    resources = unpacked / "resources"
    require(resources.is_dir(), "Installer resources are missing")
    return resources, sha(archive)


def asar_node(root, module, archive, mode, destination=None):
    # The asar implementation is installed by npm ci from the checked-in lock.
    code = r"""const fs=require('fs'),path=require('path'),crypto=require('crypto');
const asar=require(process.argv[1]),archive=process.argv[2],mode=process.argv[3];
const paths=asar.listPackage(archive);
for(const name of paths){const rel=name.replace(/^[\\/]/,'');
 if(path.win32.isAbsolute(rel)||rel.split(/[\\/]/).includes('..')||rel.includes(':'))throw Error('Unsafe asar path');
 const stat=asar.statFile(archive,rel);if(stat.link)throw Error('Asar links are forbidden');}
if(mode==='extract'){asar.extractAll(archive,process.argv[4]);}
else{const names=['main.js','lifecycle.js','updater.js','preload.js','start.js'];
 const hashes=Object.fromEntries(names.map(n=>['app/'+n,crypto.createHash('sha256').update(asar.extractFile(archive,n)).digest('hex')]));
 const p=JSON.parse(asar.extractFile(archive,'package.json'));
 const updater=JSON.parse(asar.extractFile(archive,path.join('node_modules','electron-updater','package.json')));
 console.log(JSON.stringify({hashes,version:p.version,updaterVersion:updater.version}));}
"""
    command = ["node", "-e", code, module, archive, mode]
    if destination:
        command.append(destination)
    try:
        output = run(command, cwd=root, capture_output=True, text=True).stdout
    except subprocess.CalledProcessError as error:
        raise RuntimeError("Asar operation failed: " + error.stderr.strip()) from None
    return json.loads(output) if mode == "inspect" else None


def check_resources(resources):
    resources = long_path(resources)
    require(sha(resources / "host/mc-launcher-host.exe") == HOST_SHA256, "Frozen host hash changed")
    require(read_json(resources / "root/harness/state.json") == NEUTRAL_OVERLAY, "Non-neutral UI seed")
    seeds = resources / "root/default-state"
    expected = set(NEUTRAL_SEEDS) | {"versions/" + name for name in MANIFESTS}
    require(set(hashes(seeds)) == expected, "Unexpected configuration/account seed files")
    for name, value in NEUTRAL_SEEDS.items():
        require(read_json(seeds / name) == value, "Non-neutral configuration seed: " + name)
    for name, digest in MANIFESTS.items():
        require(sha(seeds / "versions" / name) == digest, "Seed manifest changed: " + name)
    require(not (resources / "root/harness/capes").exists(), "Custom capes are forbidden")
    skins = resources / "root/harness/assets/skins"
    require({p.name for p in skins.iterdir()} == {"alex.png", "alex_avatar.png", "steve.png", "steve_avatar.png"},
            "Only the standard skin seeds are allowed")
    for name in ("offline_accounts.json", "msa_tokens.json", "microsoft-client-id.txt"):
        require(not list(resources.rglob(name)), "Personal account material is forbidden")
    require(len(SANITIZED_PNGS) == 83, "Sanitized image baseline is incomplete")
    for name, digest in SANITIZED_PNGS.items():
        file = resources / "root" / name
        require(sha(file) == digest, "Sanitized PNG changed: " + name)
        data = file.read_bytes()
        require(data.startswith(b"\x89PNG\r\n\x1a\n"), "Invalid PNG")
        position = 8
        while position < len(data):
            require(position + 12 <= len(data), "Truncated PNG")
            length = struct.unpack(">I", data[position:position + 4])[0]
            require(data[position + 4:position + 8] not in (b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME"),
                    "Identifying PNG metadata remains")
            position += length + 12
        require(position == len(data), "Truncated PNG payload")


def privacy_scan(resources):
    resources = long_path(resources)
    private_name = REPOSITORY.split("/")[0].removeprefix("abdul").removesuffix("work")
    needles = [private_name, "c:\\users\\", "c:/users/", "c:\\\\users\\\\"]
    owner = REPOSITORY.split("/")[0]
    example = r"c:\\users\\me\\appdata\\roaming\\.minecraft"
    count = 0
    for file in resources.rglob("*"):
        if not file.is_file():
            continue
        data = file.read_bytes().lower()
        for encoding in ("utf-8", "utf-16le"):
            data = data.replace(owner.encode(encoding), b"<public-owner>")
            if file.name == "app.asar":
                data = data.replace(example.encode(encoding), b"<generic-path-example>")
        require(not any(n.encode(e) in data for n in needles for e in ("utf-8", "utf-16le")),
                "Personal data match in resource: " + file.relative_to(resources).as_posix())
        count += 1
    return count


def preflight(args):
    require(re.fullmatch(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)", args.version),
            "A stable numeric release version is required")
    require(os.environ.get("GITHUB_REPOSITORY", REPOSITORY) == REPOSITORY, "Wrong repository")
    require(read_json(args.root / "app/package.json")["version"] == args.version, "Version input mismatch")
    lock = read_json(args.root / "app/package-lock.json")
    require(lock["version"] == args.version and lock["packages"][""]["version"] == args.version,
            "Package lock version mismatch")
    require(lock["packages"]["node_modules/electron"]["version"] == "45.0.0", "Unexpected Electron version")
    require(lock["packages"]["node_modules/electron-updater"]["version"] == "6.8.11", "Unexpected updater version")
    require(args.seven.is_file(), "Windows 7-Zip is missing")
    require(args.asar.is_dir(), "Locked @electron/asar dependency is missing")
    if os.environ.get("GITHUB_SHA") or os.environ.get("RELEASE_SOURCE_SHA"):
        require_checkout_commit(args.controller_root, os.environ.get("GITHUB_SHA", ""), "Workflow controller")
        require_checkout_commit(args.root, os.environ.get("RELEASE_SOURCE_SHA", ""), "Release source")


def prepare(args):
    preflight(args)
    require(not args.work.exists(), "Use a fresh staging directory")
    args.work.mkdir(parents=True)
    seed = args.seed or args.work / "seed.exe"
    if not args.seed:
        with urllib.request.urlopen(SEED_URL, timeout=120) as response, seed.open("wb") as output:
            shutil.copyfileobj(response, output, 1024 * 1024)
    require(sha(seed) == SEED_SHA256, "Public seed SHA256 mismatch")
    resources, _ = extract_payload(seed, args.work / "seed-content", args.seven)
    baseline = hashes(resources)
    require(len(baseline) == 2161, "Unexpected public resource count")
    check_resources(resources)
    require(privacy_scan(resources) == 2161, "Incomplete seed privacy scan")
    stage = args.work / "stage"
    stage.mkdir()
    shutil.copytree(resources, long_path(stage / "resources"))
    asar_node(args.root, args.asar, resources / "app.asar", "extract", stage / "app")
    runtime = {}
    for name in APP_FILES:
        source = args.root / "app" / name
        shutil.copy2(source, stage / "app" / name)
        runtime["app/" + name] = sha(source)
    for name in HARNESS_FILES:
        source = args.root / "harness" / name
        shutil.copy2(source, stage / "resources/root/harness" / name)
        runtime["harness/" + name] = sha(source)
    package = read_json(args.root / "app/package.json")
    build = package["build"]
    build["directories"] = {"output": str((args.work / "release").resolve()), "buildResources": "../resources"}
    build["extraResources"] = [{"from": "../resources/" + name, "to": name}
                               for name in ("root", "host", "icon.ico", "app-update.yml")]
    build["win"]["icon"] = "../resources/icon.ico"
    build["electronVersion"] = "45.0.0"
    build["npmRebuild"] = False
    build["nsis"]["preCompressedFileExtensions"] = []
    write_json(stage / "app/package.json", package)
    write_json(args.work / "baseline.json", {"resources": baseline, "runtime": runtime})
    print(json.dumps({"prepared": True, "version": args.version, "resources": len(baseline)}))


def asset_names(version):
    return [f"Minecraft-Launcher-Setup-{version}.exe", f"Minecraft-Launcher-{version}-Portable.exe",
            f"Minecraft-Launcher-{version}-release.json", "latest.yml", "SHA256SUMS.txt"]


def verify(args):
    preflight(args)
    release = args.release or args.work / "release"
    resources = release / "win-unpacked/resources"
    baseline = read_json(args.work / "baseline.json")
    actual = hashes(resources)
    require(len(actual) == 2161 and set(actual) == set(baseline["resources"]), "Resource inventory changed")
    changed = {"app.asar"} | {"root/harness/" + name for name in HARNESS_FILES}
    for name in set(actual) - changed:
        require(actual[name] == baseline["resources"][name], "Neutral resource changed: " + name)
    packed = asar_node(args.root, args.asar, resources / "app.asar", "inspect")
    require(packed["version"] == args.version and packed["updaterVersion"] == "6.8.11", "Packaged version mismatch")
    runtime = packed["hashes"]
    for name in HARNESS_FILES:
        runtime["harness/" + name] = sha(resources / "root/harness" / name)
    require(runtime == baseline["runtime"], "Packaged runtime differs from reviewed source")
    for name, digest in runtime.items():
        require(sha(args.root / name) == digest, "Source changed after staging: " + name)
    check_resources(resources)
    scanned = privacy_scan(resources)
    for name in ("LICENSE.electron.txt", "LICENSES.chromium.html"):
        require((release / "win-unpacked" / name).is_file(), "Missing Electron dependency notice")
    proofs = {}
    for name in asset_names(args.version)[:2]:
        with tempfile.TemporaryDirectory(prefix="payload-", dir=long_path(args.work)) as temporary:
            payload, payload_sha = extract_payload(release / name, Path(temporary) / "content", args.seven)
            require(hashes(payload) == actual, "Actual installer resource bytes differ: " + name)
            for notice in ("LICENSE.electron.txt", "LICENSES.chromium.html"):
                require(sha(payload.parent / notice) == sha(release / "win-unpacked" / notice),
                        "Installer dependency notice differs")
            proofs[name] = {"resourcesMatched": len(actual), "payloadSha256": payload_sha,
                            "installerSha256": sha(release / name)}
    names = asset_names(args.version)
    if not args.release:
        run(["node", args.root / "app/release-metadata.js", release, args.version], capture_output=True)
        checksums = "".join(f"{sha(release / name)}  {name}\n" for name in names[:-1])
        (release / "SHA256SUMS.txt").write_text(checksums, encoding="utf-8")
    else:
        checksums = "".join(f"{sha(release / name)}  {name}\n" for name in names[:-1])
        require((release / "SHA256SUMS.txt").read_text(encoding="utf-8") == checksums,
                "Existing build checksums differ")
    assets = {name: {"size": (release / name).stat().st_size, "sha256": sha(release / name)} for name in names}
    manifest = read_json(release / names[2])
    require(manifest["installer"]["sha256"] == assets[names[0]]["sha256"] and
            manifest["installer"]["size"] == assets[names[0]]["size"], "Metadata mismatch")
    digest = hashlib.sha512()
    with (release / names[0]).open("rb") as stream:
        while block := stream.read(1024 * 1024):
            digest.update(block)
    require(manifest["version"] == args.version and manifest["installer"]["filename"] == names[0]
            and manifest["installer"]["sha512"] == base64.b64encode(digest.digest()).decode(),
            "Installer SHA512 metadata mismatch")
    report = {"passed": True, "version": args.version, **release_provenance(),
              "repository": REPOSITORY, "seedInstallerSha256": SEED_SHA256,
              "frozenHostSha256": HOST_SHA256, "runtimeSha256": runtime,
              "resourceCount": len(actual), "resourceTreeSha256": tree_sha(actual),
              "neutralResourceBytesUnchanged": True, "neutralSeedsVerified": True,
              "sanitizedPngsUnchanged": len(SANITIZED_PNGS), "resourceFilesPrivacyScanned": scanned,
              "privateMatches": [], "dependencyNoticesPresent": True, "payloads": proofs, "assets": assets}
    write_json((args.work if args.release else release) / "ci-release-report.json", report)
    print(json.dumps({"passed": True, "resourcesMatchedInEachPayload": len(actual), "assets": assets}))


def api(path, method="GET", value=None):
    token = os.environ.get("GITHUB_TOKEN", "")
    require(bool(token), "Ephemeral GITHUB_TOKEN is required for publication")
    request = urllib.request.Request("https://api.github.com" + path, method=method,
                                    data=json.dumps(value).encode() if value is not None else None,
                                    headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                                             "X-GitHub-Api-Version": "2022-11-28", "Content-Type": "application/json",
                                             "User-Agent": "minecraft-launcher-release-ci"})
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            body = response.read()
            return json.loads(body) if body else None
    except urllib.error.HTTPError as error:
        if error.code == 404 and method == "GET":
            return None
        raise RuntimeError(f"GitHub API {method} failed with HTTP {error.code}") from None


def require_task_draft(release, tag, names):
    state = {"id": release.get("id"), "tag_name": str(release.get("tag_name", ""))[:64],
             "draft": release.get("draft"), "prerelease": release.get("prerelease")}
    require(release["tag_name"] == tag and release["draft"] and not release["prerelease"],
            "Only this version's unpublished stable draft may be changed: " + json.dumps(state))
    require(all(asset["name"] in names for asset in release["assets"]), "Unexpected draft assets; inspect manually")
    require(len({a["name"] for a in release["assets"]}) == len(release["assets"]), "Duplicate draft assets")


def find_task_release(prefix, tag, request=api):
    # Drafts can be invisible to GET /releases/tags/{tag}; authenticated release
    # listing includes them. Search all bounded
    # pages before allowing creation, and refuse ambiguous exact-tag matches.
    matches = []
    for page in range(1, 21):
        releases = request(prefix + f"/releases?per_page=100&page={page}")
        require(isinstance(releases, list), "Release listing was unavailable")
        matches.extend(release for release in releases if release.get("tag_name") == tag)
        require(len(matches) <= 1, "Multiple releases have the requested tag; inspect manually")
        if len(releases) < 100:
            return matches[0] if matches else None
    raise RuntimeError("Release listing exceeded the safety bound; creation is disabled")


def require_source_tag(prefix, tag, commit, request=api):
    ref = request(prefix + "/git/ref/tags/" + tag)
    if ref is None:
        request(prefix + "/git/refs", "POST", {"ref": "refs/tags/" + tag, "sha": commit})
        ref = request(prefix + "/git/ref/tags/" + tag)
    obj = ref["object"]
    for _ in range(4):
        if obj["type"] != "tag":
            break
        obj = request(prefix + "/git/tags/" + obj["sha"])["object"]
    require(obj["type"] == "commit" and obj["sha"] == commit, "Version tag differs from the release source commit")


def publish(args):
    preflight(args)
    commit = os.environ.get("RELEASE_SOURCE_SHA", "")
    workflow_commit = os.environ.get("GITHUB_SHA", "")
    require(re.fullmatch(r"[0-9a-f]{40}", commit), "Publication requires the release source commit")
    require(re.fullmatch(r"[0-9a-f]{40}", workflow_commit), "Publication requires the workflow commit")
    require(os.environ.get("GITHUB_ACTIONS") == "true", "Publication is restricted to the Actions runner")
    release_dir = args.work / "release"
    report = read_json(release_dir / "ci-release-report.json")
    require_report_provenance(report, args.version, commit, workflow_commit)
    names = asset_names(args.version)
    require(set(report["assets"]) == set(names), "Verified asset list is incomplete")
    for name in names:
        file = release_dir / name
        require(sha(file) == report["assets"][name]["sha256"] and file.stat().st_size == report["assets"][name]["size"],
                "Asset changed after verification")
    prefix = "/repos/" + REPOSITORY
    repo = api(prefix)
    require(repo["id"] == REPOSITORY_ID and repo["full_name"] == REPOSITORY, "Repository identity mismatch")
    tag = "v" + args.version
    release = find_task_release(prefix, tag)
    if release is None:
        release = api(prefix + "/releases", "POST", {"tag_name": tag, "target_commitish": commit,
                      "name": "Minecraft Launcher " + args.version, "draft": True, "prerelease": False,
                      "body": "Verified Windows installers with GitHub update support. Both payloads preserve the neutral public resource seeds.",
                      "generate_release_notes": False})
    require_task_draft(release, tag, names)
    require_source_tag(prefix, tag, commit)
    rid = release["id"]
    release = api(prefix + "/releases/" + str(rid), "PATCH", {"target_commitish": commit})
    require_task_draft(release, tag, names)
    existing = {a["name"]: a for a in release["assets"]}
    for name in names:
        expected = report["assets"][name]
        old = existing.get(name)
        if old and old["state"] == "uploaded" and old["size"] == expected["size"] and old.get("digest") == "sha256:" + expected["sha256"]:
            continue
        # Recheck draft state before every mutation; published releases are never overwritten.
        require_task_draft(api(prefix + "/releases/" + str(rid)), tag, names)
        if old:
            api(prefix + "/releases/assets/" + str(old["id"]), "DELETE")
        env = dict(os.environ, GH_TOKEN=os.environ["GITHUB_TOKEN"], GH_HOST="github.com")
        run(["gh", "release", "upload", tag, release_dir / name, "--repo", REPOSITORY], env=env)
        print(json.dumps({"uploaded": name}))
    remote = api(prefix + "/releases/" + str(rid))
    require_task_draft(remote, tag, names)
    require({a["name"] for a in remote["assets"]} == set(names), "Draft asset set is incomplete")
    for asset in remote["assets"]:
        expected = report["assets"][asset["name"]]
        require(asset["state"] == "uploaded" and asset["size"] == expected["size"] and
                asset.get("digest") == "sha256:" + expected["sha256"], "GitHub asset digest mismatch")
    require_source_tag(prefix, tag, commit)
    final = api(prefix + "/releases/" + str(rid), "PATCH", {"draft": False, "make_latest": "true"})
    require(not final["draft"] and final["tag_name"] == tag, "Publication response mismatch")
    print(json.dumps({"published": final["html_url"], **release_provenance(), "remoteSha256Verified": True}))


def self_test(args):
    safe_archive_listing("Path = resources\\root\\safe.js\nPath = $PLUGINSDIR\\app-64.7z")
    for bad in ("../escape", r"C:\escape", r"\escape", "file:stream", r"resources\..\escape"):
        try:
            safe_archive_listing("Path = " + bad)
        except RuntimeError:
            continue
        raise RuntimeError("Unsafe path was accepted")
    for bad in ("Symbolic Link = outside", "Hard Link = outside"):
        try:
            safe_archive_listing(bad)
        except RuntimeError:
            continue
        raise RuntimeError("Unsafe link was accepted")
    names = asset_names("3.41.31")
    draft = {"tag_name": "v3.41.31", "draft": True, "prerelease": False, "assets": []}
    require_task_draft(draft, "v3.41.31", names)
    for field, value in (("draft", False), ("tag_name", "v3.41.30"), ("prerelease", True),
                         ("assets", [{"name": "unrelated.exe"}])):
        try:
            require_task_draft(dict(draft, **{field: value}), "v3.41.31", names)
        except RuntimeError:
            continue
        raise RuntimeError("Unsafe draft state was accepted")
    calls = []
    untagged_draft = dict(draft, id=441772528)
    def draft_listing(path, method="GET", value=None):
        calls.append((path, method))
        require(method == "GET", "Draft discovery attempted a mutation")
        if "/releases/tags/" in path:
            return None  # Real API behavior for an existing untagged draft.
        return [untagged_draft]
    require(draft_listing("/repos/example/launcher/releases/tags/v3.41.31") is None,
            "Untagged draft fixture did not reproduce the tag lookup miss")
    require(find_task_release("/repos/example/launcher", "v3.41.31", draft_listing) == untagged_draft,
            "Existing untagged draft was not selected")
    require(all(method == "GET" for _, method in calls), "Draft discovery attempted creation")
    def duplicate_listing(path):
        return [untagged_draft, dict(untagged_draft, id=441772529)]
    try:
        find_task_release("/repos/example/launcher", "v3.41.31", duplicate_listing)
    except RuntimeError:
        pass
    else:
        raise RuntimeError("Duplicate exact-tag releases were accepted")
    def paginated_listing(path):
        if path.endswith("page=1"):
            return [{"tag_name": "v0.0." + str(n)} for n in range(100)]
        return [untagged_draft]
    require(find_task_release("/repos/example/launcher", "v3.41.31", paginated_listing) == untagged_draft,
            "Existing draft on the second page was missed")
    with tempfile.TemporaryDirectory(prefix="ci-release-guard-") as temporary:
        checkout = Path(temporary)
        run(["git", "init", "--quiet", checkout], capture_output=True)
        file = checkout / "runtime.js"
        file.write_text("reviewed\n", encoding="utf-8")
        run(["git", "add", "runtime.js"], cwd=checkout, capture_output=True)
        run(["git", "-c", "user.name=CI release guard", "-c", "user.email=ci-release@example.invalid",
             "commit", "--quiet", "-m", "Guard fixture"], cwd=checkout, capture_output=True)
        require_clean_checkout(checkout)
        controller_commit = run(["git", "rev-parse", "HEAD"], cwd=checkout,
                                capture_output=True, text=True).stdout.strip()
        source = checkout / ".release-source"
        run(["git", "init", "--quiet", source], capture_output=True)
        source_file = source / "runtime.js"
        source_file.write_text("release source\n", encoding="utf-8")
        run(["git", "add", "runtime.js"], cwd=source, capture_output=True)
        run(["git", "-c", "user.name=CI release guard", "-c", "user.email=ci-release@example.invalid",
             "commit", "--quiet", "-m", "Release source fixture"], cwd=source, capture_output=True)
        source_commit = run(["git", "rev-parse", "HEAD"], cwd=source,
                            capture_output=True, text=True).stdout.strip()
        require(source_commit != controller_commit, "Fixture source and workflow commits must differ")
        require_checkout_commit(checkout, controller_commit, "Workflow controller")
        require_checkout_commit(source, source_commit, "Release source")
        try:
            require_checkout_commit(source, controller_commit, "Release source")
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Wrong release source SHA was accepted")
        proof = {"passed": True, "version": "3.41.31", **release_provenance(source_commit, controller_commit)}
        require_report_provenance(proof, "3.41.31", source_commit, controller_commit)
        for field, value in (("sourceCommit", controller_commit), ("workflowCommit", source_commit)):
            try:
                require_report_provenance(dict(proof, **{field: value}), "3.41.31", source_commit, controller_commit)
            except RuntimeError:
                continue
            raise RuntimeError("Source/workflow report substitution was accepted")
        tag_calls = []
        def existing_source_tag(path, method="GET", value=None):
            tag_calls.append(method)
            require(method == "GET", "Existing release source tag must not be moved")
            return {"object": {"type": "commit", "sha": source_commit}}
        require_source_tag("/repos/example/launcher", "v3.41.31", source_commit, existing_source_tag)
        try:
            require_source_tag("/repos/example/launcher", "v3.41.31", controller_commit, existing_source_tag)
        except RuntimeError:
            pass
        else:
            raise RuntimeError("Workflow SHA was accepted in place of the release source tag")
        require(all(method == "GET" for method in tag_calls), "Existing tag was mutated")
        (checkout / ".ci-release").mkdir()
        (checkout / ".ci-release/artifact.json").write_text("{}", encoding="utf-8")
        require_clean_checkout(checkout)
        file.write_text("modified\n", encoding="utf-8")
        for staged in (False, True):
            if staged:
                run(["git", "add", "runtime.js"], cwd=checkout, capture_output=True)
            require_checkout_commit(source, source_commit, "Release source")
            try:
                require_checkout_commit(checkout, controller_commit, "Workflow controller")
            except RuntimeError:
                continue
            raise RuntimeError("Dirty tracked checkout was accepted")
        run(["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", "runtime.js"],
            cwd=checkout, capture_output=True)
        source_file.write_text("dirty release source\n", encoding="utf-8")
        for staged in (False, True):
            if staged:
                run(["git", "add", "runtime.js"], cwd=source, capture_output=True)
            require_checkout_commit(checkout, controller_commit, "Workflow controller")
            try:
                require_checkout_commit(source, source_commit, "Release source")
            except RuntimeError:
                continue
            raise RuntimeError("Dirty release source checkout was accepted")
    print(json.dumps({"passed": True, "archiveAndPublicationGuards": True, "trackedCheckoutGuard": True,
                      "untaggedDraftLookup": True, "duplicateDraftsRejected": True, "releaseListingPagination": True,
                      "sourceAndWorkflowChecks": True, "sourceTagImmutable": True, "reportProvenanceSeparated": True}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("preflight", "prepare", "verify", "publish", "self-test"))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--work", type=Path)
    parser.add_argument("--seed", type=Path, help="Local pinned seed for an offline dry run")
    parser.add_argument("--release", type=Path, help="Existing local build for payload verification")
    parser.add_argument("--asar", type=Path, help="Installed lock-compatible asar module for a local dry run")
    parser.add_argument("--seven", type=Path, default=Path("C:/Program Files/7-Zip/7z.exe"))
    parser.add_argument("--version", default=os.environ.get("RELEASE_VERSION", ""))
    args = parser.parse_args()
    args.controller_root = Path(__file__).resolve().parents[1]
    args.root = args.root.resolve()
    args.work = (args.work or args.controller_root / ".ci-release").resolve()
    args.seed = args.seed.resolve() if args.seed else None
    args.release = args.release.resolve() if args.release else None
    args.asar = (args.asar or args.root / "app/node_modules/@electron/asar").resolve()
    args.seven = args.seven.resolve()
    require(args.work != args.root and not args.root.is_relative_to(args.work), "Invalid staging directory")
    require(args.work != args.controller_root and not args.controller_root.is_relative_to(args.work),
            "Invalid controller staging directory")
    {"preflight": preflight, "prepare": prepare, "verify": verify,
     "publish": publish, "self-test": self_test}[args.command](args)


if __name__ == "__main__":
    main()
