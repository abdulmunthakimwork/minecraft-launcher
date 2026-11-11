"""Remove identifying PNG ancillary metadata while preserving rendering chunks."""
import hashlib, json, struct, sys, zlib
from pathlib import Path

resource_root=Path(sys.argv[1]).resolve()
if not resource_root.is_dir():raise SystemExit('PNG resource directory is missing')
remove={b'tEXt',b'zTXt',b'iTXt',b'eXIf',b'tIME'}
report={'passed':True,'filesChanged':0,'removedChunks':{},'renderingChunksUnchanged':True,'files':[]}
for file in resource_root.rglob('*.png'):
    original=file.read_bytes()
    if original[:8] != b'\x89PNG\r\n\x1a\n':continue
    pos=8;retained=[original[:8]];removed=[]
    while pos<len(original):
        if pos+12>len(original):raise ValueError('Truncated PNG chunk: '+str(file))
        size=struct.unpack('>I',original[pos:pos+4])[0]
        kind=original[pos+4:pos+8]
        end=pos+12+size
        if end>len(original):raise ValueError('Truncated PNG payload: '+str(file))
        payload=original[pos+8:pos+8+size]
        crc=struct.unpack('>I',original[pos+8+size:end])[0]
        if zlib.crc32(kind+payload)&0xffffffff != crc:raise ValueError('Invalid PNG CRC: '+str(file))
        if kind in remove:
            removed.append(kind.decode())
            report['removedChunks'][kind.decode()]=report['removedChunks'].get(kind.decode(),0)+1
        else:retained.append(original[pos:end])
        pos=end
    if not removed:continue
    sanitized=b''.join(retained)
    # No retained byte is recompressed or changed. Pixel/color chunks and CRCs
    # remain verbatim, proving the rendered image is unchanged.
    file.write_bytes(sanitized)
    report['filesChanged']+=1
    report['files'].append({'path':file.relative_to(resource_root).as_posix(),
        'removedChunks':removed,'sha256':hashlib.sha256(sanitized).hexdigest()})
report_path=Path(sys.argv[2]) if len(sys.argv)>2 else resource_root.parent/'png-privacy-report.json'
report_path.write_text(json.dumps(report,indent=2))
print(json.dumps({k:v for k,v in report.items() if k!='files'}))
