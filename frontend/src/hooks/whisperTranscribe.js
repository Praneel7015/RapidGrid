/**
 * Firefox (and any browser without the Web Speech API) records a clip and
 * transcribes it here. The Whisper model stays in the browser and is cached
 * after the first download.
 */

let transcriberPromise = null;

function preferredMime() {
  if (typeof MediaRecorder === 'undefined') return '';
  const types = [
    'audio/ogg;codecs=opus',
    'audio/webm;codecs=opus',
    'audio/webm',
    'audio/mp4',
  ];
  return types.find((type) => MediaRecorder.isTypeSupported(type)) || '';
}

export function recorderOptions() {
  const mimeType = preferredMime();
  return mimeType ? { mimeType } : undefined;
}

export function preloadWhisper() {
  if (!transcriberPromise) {
    transcriberPromise = import('@huggingface/transformers')
      .then(async ({ pipeline, env }) => {
        env.allowLocalModels = false;
        env.useBrowserCache = true;
        const wasm = env.backends?.onnx?.wasm;
        if (wasm) wasm.numThreads = 1;
        return pipeline('automatic-speech-recognition', 'onnx-community/whisper-tiny.en', {
          dtype: 'q8',
          device: 'wasm',
        });
      })
      .catch((err) => {
        transcriberPromise = null;
        throw err;
      });
  }
  return transcriberPromise;
}

function transcriptOf(result) {
  if (!result) return '';
  if (typeof result === 'string') return result.trim();
  if (Array.isArray(result)) {
    return result.map((part) => part?.text || '').join(' ').trim();
  }
  return String(result.text || '').trim();
}

async function decodeTo16kMono(blob) {
  const audioCtx = new AudioContext();
  try {
    const decoded = await audioCtx.decodeAudioData(await blob.arrayBuffer());
    const length = Math.max(1, Math.ceil(decoded.duration * 16000));
    const offline = new OfflineAudioContext(1, length, 16000);
    const source = offline.createBufferSource();
    source.buffer = decoded;
    source.connect(offline.destination);
    source.start();
    const rendered = await offline.startRendering();
    return new Float32Array(rendered.getChannelData(0));
  } finally {
    await audioCtx.close();
  }
}

export async function transcribeRecording(blob) {
  const samples = await decodeTo16kMono(blob);
  if (samples.length < 1600) return '';
  const transcriber = await preloadWhisper();
  // Whisper sees a 30 s window; chunk longer clips so nothing after it is dropped.
  const result = await transcriber(samples, { chunk_length_s: 30, stride_length_s: 5 });
  return transcriptOf(result);
}
