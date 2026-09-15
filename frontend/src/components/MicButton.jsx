/**
 * Mic toggle for Web Speech dictation.
 */

import React from 'react';
import { Mic, MicOff } from 'lucide-react';

export default function MicButton({
  supported,
  listening,
  onToggle,
  title,
  className = '',
  size = 15,
}) {
  if (!supported) {
    return (
      <button
        type="button"
        disabled
        title="Voice input is not supported in this browser"
        aria-label="Voice input unavailable"
        className={`tap grid h-11 w-11 shrink-0 place-items-center rounded-sm border border-rule text-text-faint opacity-50 lg:h-9 lg:w-9 ${className}`}
      >
        <MicOff size={size} />
      </button>
    );
  }

  return (
    <button
      type="button"
      onClick={onToggle}
      title={title ?? (listening ? 'Stop listening' : 'Dictate with voice')}
      aria-label={listening ? 'Stop voice input' : 'Start voice input'}
      aria-pressed={listening}
      className={`tap grid h-11 w-11 shrink-0 place-items-center rounded-sm border transition-colors lg:h-9 lg:w-9 ${
        listening
          ? 'border-critical bg-critical text-on-ink'
          : 'border-ink bg-paper text-text hover:bg-ink hover:text-on-ink'
      } ${className}`}
    >
      <Mic size={size} className={listening ? 'live-dot' : ''} />
    </button>
  );
}
