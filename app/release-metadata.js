const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');

async function createReleaseMetadata(directory, version) {
  if (!/^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$/.test(version)) throw new Error('A stable release version is required.');
  const filename = `Minecraft-Launcher-Setup-${version}.exe`;
  const installer = path.join(directory, filename);
  const stat = await fs.promises.lstat(installer);
  if (!stat.isFile() || stat.isSymbolicLink() || stat.size <= 0) throw new Error('The release installer is missing or empty.');
  const sha256 = crypto.createHash('sha256'), sha512 = crypto.createHash('sha512');
  for await (const chunk of fs.createReadStream(installer)) {sha256.update(chunk); sha512.update(chunk);}
  const checksums = {sha256: sha256.digest('hex'), sha512: sha512.digest('base64')};
  const manifest = {schemaVersion: 1, appId: 'app.mclauncher.desktop', version,
    installer: {filename, size: stat.size, ...checksums}, updateMetadata: 'latest.yml'};
  const manifestName = `Minecraft-Launcher-${version}-release.json`;
  const metadata = `version: ${version}\nfiles:\n  - url: ${JSON.stringify(filename)}\n    sha512: ${checksums.sha512}\n    size: ${stat.size}\npath: ${JSON.stringify(filename)}\nsha512: ${checksums.sha512}\nreleaseDate: ${JSON.stringify(new Date().toISOString())}\n`;
  await fs.promises.writeFile(path.join(directory, manifestName), JSON.stringify(manifest, null, 2) + '\n');
  await fs.promises.writeFile(path.join(directory, 'latest.yml'), metadata);
  return {manifest: path.join(directory, manifestName), metadata: path.join(directory, 'latest.yml')};
}

if (require.main === module) {
  const directory = path.resolve(process.argv[2] || path.join(__dirname, '..', 'release'));
  const version = process.argv[3] || require('./package.json').version;
  createReleaseMetadata(directory, version).then(result => {
    process.stdout.write(JSON.stringify(result, null, 2) + '\n');
  }).catch(error => {process.stderr.write('Release metadata failed: ' + error.message + '\n'); process.exitCode = 1;});
}

module.exports = {createReleaseMetadata};
