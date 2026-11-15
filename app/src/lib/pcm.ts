/** Понижение частоты с усреднением (простой фильтр от наложения) и перевод в PCM16. */
export function downsampleToInt16(input: Float32Array, inRate: number, outRate = 16000): Int16Array {
  if (outRate > inRate) throw new Error("повышение частоты не поддерживается");
  const ratio = inRate / outRate;
  const outLen = Math.floor(input.length / ratio);
  const out = new Int16Array(outLen);
  for (let i = 0; i < outLen; i++) {
    const start = Math.floor(i * ratio);
    const end = Math.min(input.length, Math.floor((i + 1) * ratio));
    let sum = 0;
    for (let j = start; j < end; j++) sum += input[j];
    const v = Math.max(-1, Math.min(1, sum / Math.max(1, end - start)));
    out[i] = v < 0 ? Math.round(v * 0x8000) : Math.round(v * 0x7fff);
  }
  return out;
}

export function int16ToFloat32(buf: ArrayBuffer): Float32Array<ArrayBuffer> {
  const src = new Int16Array(buf, 0, Math.floor(buf.byteLength / 2));
  const out = new Float32Array(src.length);
  for (let i = 0; i < src.length; i++) out[i] = src[i] / 0x8000;
  return out;
}

export function concat(chunks: Float32Array[]): Float32Array {
  const out = new Float32Array(chunks.reduce((n, c) => n + c.length, 0));
  let off = 0;
  for (const c of chunks) {
    out.set(c, off);
    off += c.length;
  }
  return out;
}
