import { sttStreamUrl } from "./api";

/**
 * Microphone capture for streaming STT.
 *
 * This module is deliberately thin: it opens a microphone, converts its audio
 * to 16 kHz mono PCM and streams it over a WebSocket. Every decision about
 * interim text, word deltas, end-of-utterance and the final transcript is made
 * by the Python backend (server/routes/stt.py).
 */

export interface AudioCaptureCallbacks {
  /** Newly recognised words and the full committed caption. */
  onPartial?: (delta: string, text: string) => void;
  /** Backend finalised the utterance (manual stop or detected pause). */
  onFinal?: (text: string) => void;
  /** Backend or transport failure. */
  onError?: (message: string) => void;
  /** Live microphone analyser for the voice-mode visualizer; null on cleanup. */
  onAnalyser?: (analyser: AnalyserNode | null) => void;
}

interface ActiveCapture {
  stop(): Promise<string>;
}

let active: ActiveCapture | null = null;

/** Matches server/routes/stt.py::_SAMPLE_RATE. */
const _SAMPLE_RATE = 16000;

/**
 * AudioWorklet module. Averages the input down to 16 kHz and posts Int16
 * frames on a transferable buffer, so no copying happens on the main thread.
 */
const PCM_WORKLET = `
class PcmCapture extends AudioWorkletProcessor {
  constructor() {
    super();
    this.ratio = sampleRate / 16000;
    this.sum = 0;
    this.count = 0;
    this.buffer = new Int16Array(2048);
    this.pos = 0;
  }
  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        this.sum += channel[i];
        this.count++;
        if (this.count >= this.ratio) {
          let value = Math.max(-1, Math.min(1, this.sum / this.count));
          this.buffer[this.pos++] = value < 0 ? value * 32768 : value * 32767;
          this.sum = 0;
          this.count = 0;
          if (this.pos === this.buffer.length) {
            const frame = this.buffer.slice();
            this.port.postMessage(frame, [frame.buffer]);
            this.pos = 0;
          }
        }
      }
    }
    return true;
  }
}
registerProcessor('pcm-capture', PcmCapture);
`;

interface OpenedMic {
  stream: MediaStream;
  label: string;
}

/**
 * Open a microphone that actually works.
 *
 * The OS default is tried first — it is what users expect. When it is absent
 * or busy, WebView2 reports NotFoundError/NotReadableError; we then enumerate
 * the real input devices and try each explicitly instead of failing outright.
 */
async function openMicrophone(): Promise<OpenedMic> {
  if (!navigator.mediaDevices?.getUserMedia) {
    throw new Error("Микрофон недоступен в этом окне (mediaDevices missing)");
  }

  const failures: string[] = [];
  try {
    const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
    const label = stream.getAudioTracks()[0]?.label || "Микрофон по умолчанию";
    return { stream, label };
  } catch (e: any) {
    failures.push(`по умолчанию: ${e?.message || e}`);
  }

  let devices: MediaDeviceInfo[] = [];
  try {
    devices = await navigator.mediaDevices.enumerateDevices();
  } catch (e: any) {
    failures.push(`перечисление: ${e?.message || e}`);
  }

  for (const device of devices) {
    if (device.kind !== "audioinput" || !device.deviceId || device.deviceId === "default") {
      continue;
    }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { deviceId: { exact: device.deviceId } },
      });
      return {
        stream,
        label: device.label || stream.getAudioTracks()[0]?.label || device.deviceId.slice(0, 12),
      };
    } catch (e: any) {
      failures.push(`${device.label || "устройство"}: ${e?.message || e}`);
    }
  }

  throw new Error(`Нет доступного микрофона. ${failures.join(" | ")}`);
}

function connectWebSocket(url: string): Promise<WebSocket> {
  return new Promise((resolve, reject) => {
    const socket = new WebSocket(url);
    const timer = window.setTimeout(() => {
      socket.close();
      reject(new Error("STT-бэкенд не ответил вовремя"));
    }, 8000);
    socket.onopen = () => {
      window.clearTimeout(timer);
      resolve(socket);
    };
    socket.onerror = () => {
      window.clearTimeout(timer);
      reject(new Error("Не удалось подключиться к STT-бэкенду"));
    };
  });
}

/** Returns the label of the microphone that was actually opened. */
export async function startAudioCapture(
  callbacks: AudioCaptureCallbacks = {},
): Promise<string> {
  if (active) return "";

  const mic = await openMicrophone();
  const sessionId = `stt-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

  let socket: WebSocket;
  let audio: AudioContext | null = null;
  let analyser: AnalyserNode | null = null;
  try {
    socket = await connectWebSocket(await sttStreamUrl(sessionId));
  } catch (e) {
    mic.stream.getTracks().forEach((t) => t.stop());
    throw e;
  }

  let resolveFinal!: (text: string) => void;
  let rejectFinal!: (error: Error) => void;
  const finalText = new Promise<string>((resolve, reject) => {
    resolveFinal = resolve;
    rejectFinal = reject;
  });

  let cleanedUp = false;
  let capture: ActiveCapture | null = null;
  const cleanup = () => {
    if (cleanedUp) return;
    cleanedUp = true;
    // A backend-detected endpoint finalizes without a manual stop. Clear the
    // module state here as well, otherwise a later stop would target a dead
    // session.
    if (active === capture) active = null;
    if (socket.readyState === WebSocket.OPEN ||
        socket.readyState === WebSocket.CONNECTING) {
      socket.close();
    }
    mic.stream.getTracks().forEach((t) => t.stop());
    callbacks.onAnalyser?.(null);
    if (audio) void audio.close();
  };

  socket.onmessage = (event: MessageEvent<string>) => {
    let message: any;
    try {
      message = JSON.parse(event.data);
    } catch {
      return;
    }
    if (message.type === "partial") {
      callbacks.onPartial?.(message.delta || "", message.text || "");
    } else if (message.type === "final") {
      cleanup();
      const text = message.text || "";
      callbacks.onFinal?.(text);
      resolveFinal(text);
    } else if (message.type === "error") {
      cleanup();
      const error = new Error(message.message || "Ошибка распознавания");
      callbacks.onError?.(error.message);
      rejectFinal(error);
    }
  };

  socket.onclose = () => {
    if (!cleanedUp) {
      cleanup();
      const error = new Error("STT-поток закрылся до завершения");
      callbacks.onError?.(error.message);
      rejectFinal(error);
    }
  };

  try {
    // Let the browser resample the microphone to the rate the backend expects.
    // The worklet then only converts floats to Int16 — no ad-hoc decimation.
    audio = new AudioContext({ sampleRate: _SAMPLE_RATE });
    await audio.resume();
    const moduleUrl = URL.createObjectURL(
      new Blob([PCM_WORKLET], { type: "application/javascript" }),
    );
    try {
      await audio.audioWorklet.addModule(moduleUrl);
    } finally {
      URL.revokeObjectURL(moduleUrl);
    }

    const source = audio.createMediaStreamSource(mic.stream);
    analyser = audio.createAnalyser();
    analyser.fftSize = 256;
    analyser.smoothingTimeConstant = 0.75;
    source.connect(analyser);
    callbacks.onAnalyser?.(analyser);
    const processor = new AudioWorkletNode(audio, "pcm-capture");
    processor.port.onmessage = (event: MessageEvent<Int16Array>) => {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(event.data.buffer);
      }
    };
    const silent = audio.createGain();
    silent.gain.value = 0;
    source.connect(processor);
    processor.connect(silent);
    silent.connect(audio.destination);
  } catch (e: any) {
    cleanup();
    throw new Error(`Не удалось запустить захват PCM: ${e?.message || e}`);
  }

  capture = {
    stop(): Promise<string> {
      if (socket.readyState === WebSocket.OPEN) {
        socket.send(JSON.stringify({ type: "stop" }));
      }
      return finalText;
    },
  };
  active = capture;

  return mic.label;
}

export async function stopAudioCapture(): Promise<string> {
  const capture = active;
  if (!capture) return "";
  active = null;
  return capture.stop();
}
