/**
 * Dictation.
 *
 * Edge and Chrome use the Web Speech API and show words as you speak.
 * Firefox has no speech recognizer, so the mic records a clip and a small
 * Whisper model in the browser transcribes it when you tap again.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import { preloadWhisper, recorderOptions, transcribeRecording } from './whisperTranscribe';

function getRecognitionCtor() {
  if (typeof window === 'undefined') return null;
  return window.SpeechRecognition || window.webkitSpeechRecognition || null;
}

function canRecord() {
  return typeof navigator !== 'undefined'
    && typeof MediaRecorder !== 'undefined'
    && Boolean(navigator.mediaDevices?.getUserMedia);
}

function failureMessage(err, phase) {
  const name = err?.name || '';
  if (name === 'NotAllowedError' || name === 'SecurityError') {
    return 'Microphone permission was blocked.';
  }
  if (name === 'NotFoundError') return 'No microphone was found.';
  if (phase === 'loading') {
    return 'Could not download the speech model. Check the connection and try again.';
  }
  return 'Could not transcribe that clip. You can still type.';
}

// Web Speech reports bare codes; show the caller a sentence instead.
const WEBSPEECH_ERRORS = {
  'not-allowed': 'Microphone permission was blocked.',
  'service-not-allowed': 'Microphone permission was blocked.',
  'audio-capture': 'No microphone was found.',
  network: 'Voice input needs a network connection. You can still type.',
};

function webSpeechMessage(code) {
  return WEBSPEECH_ERRORS[code] || 'Voice input stopped. You can still type.';
}

export default function useSpeechToText({ lang = 'en-IN', continuous = false } = {}) {
  const Ctor = getRecognitionCtor();
  const engine = Ctor ? 'webspeech' : canRecord() ? 'whisper' : null;
  const supported = engine !== null;
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState('');
  const [error, setError] = useState(null);
  const [phase, setPhase] = useState('idle');
  const recRef = useRef(null);
  const mediaRef = useRef(null);
  const onFinalRef = useRef(null);
  const startingRef = useRef(false);
  const aliveRef = useRef(true);

  const setOnFinal = useCallback((fn) => {
    onFinalRef.current = fn;
  }, []);

  const releaseMic = useCallback(() => {
    mediaRef.current?.getTracks().forEach((track) => track.stop());
    mediaRef.current = null;
  }, []);

  useEffect(() => () => {
    aliveRef.current = false;
    try { recRef.current?.stop(); } catch { /* ignore */ }
    releaseMic();
    onFinalRef.current = null;
  }, [releaseMic]);

  const stop = useCallback(() => {
    startingRef.current = false;
    try { recRef.current?.stop(); } catch { /* ignore */ }
    if (engine !== 'whisper') {
      setListening(false);
      setInterim('');
    }
  }, [engine]);

  const startWebSpeech = useCallback(() => {
    if (!Ctor || startingRef.current) return;
    startingRef.current = true;
    setError(null);
    try {
      const rec = new Ctor();
      rec.lang = lang;
      rec.continuous = continuous;
      rec.interimResults = true;
      rec.onstart = () => { startingRef.current = false; setListening(true); };
      rec.onend = () => {
        startingRef.current = false;
        setListening(false);
        setInterim('');
      };
      rec.onerror = (ev) => {
        startingRef.current = false;
        setListening(false);
        setInterim('');
        if (ev.error !== 'aborted' && ev.error !== 'no-speech') {
          setError(webSpeechMessage(ev.error));
        }
      };
      rec.onresult = (ev) => {
        let interimText = '';
        let finalText = '';
        for (let i = ev.resultIndex; i < ev.results.length; i += 1) {
          const piece = ev.results[i][0]?.transcript ?? '';
          if (ev.results[i].isFinal) finalText += piece;
          else interimText += piece;
        }
        setInterim(interimText);
        if (finalText && onFinalRef.current) onFinalRef.current(finalText.trim());
      };
      recRef.current = rec;
      rec.start();
    } catch (err) {
      startingRef.current = false;
      setError(webSpeechMessage(err?.name === 'NotAllowedError' ? 'not-allowed' : null));
      setListening(false);
    }
  }, [Ctor, continuous, lang]);

  const startWhisper = useCallback(async () => {
    if (startingRef.current || phase === 'loading' || phase === 'transcribing') return;
    startingRef.current = true;
    setError(null);
    setInterim('');
    preloadWhisper().catch(() => { /* surfaced when the clip is transcribed */ });
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
      mediaRef.current = stream;
      if (!aliveRef.current) {
        releaseMic();
        return;
      }
      const rec = new MediaRecorder(stream, recorderOptions());
      const chunks = [];
      rec.ondataavailable = (ev) => {
        if (ev.data?.size) chunks.push(ev.data);
      };
      rec.onstop = async () => {
        releaseMic();
        if (!aliveRef.current) return;
        setListening(false);
        const blob = new Blob(chunks, { type: rec.mimeType || 'audio/ogg' });
        let stage = 'loading';
        setPhase('loading');
        try {
          await preloadWhisper();
          if (!aliveRef.current) return;
          stage = 'transcribing';
          setPhase('transcribing');
          const text = await transcribeRecording(blob);
          if (!aliveRef.current) return;
          if (text && onFinalRef.current) onFinalRef.current(text);
          else setError('No speech detected. Try again.');
        } catch (err) {
          if (!aliveRef.current) return;
          setError(failureMessage(err, stage));
        } finally {
          if (aliveRef.current) setPhase('idle');
        }
      };
      mediaRef.current = stream;
      recRef.current = rec;
      rec.start();
      startingRef.current = false;
      setListening(true);
    } catch (err) {
      startingRef.current = false;
      releaseMic();
      setListening(false);
      setPhase('idle');
      setError(failureMessage(err, 'idle'));
    }
  }, [phase, releaseMic]);

  const start = useCallback(() => {
    if (engine === 'webspeech') startWebSpeech();
    else if (engine === 'whisper') startWhisper();
  }, [engine, startWebSpeech, startWhisper]);

  const toggle = useCallback(() => {
    if (phase === 'loading' || phase === 'transcribing') return;
    if (listening) stop();
    else start();
  }, [listening, phase, start, stop]);

  return { supported, engine, phase, listening, interim, error, start, stop, toggle, setOnFinal };
}
