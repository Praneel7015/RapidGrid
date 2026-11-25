/**
 * A phone number you can call. On a phone (touch screen) it is a tel: link.
 * On a laptop or desktop a tel: link makes the operating system open some
 * other application (or offer to install one), so there it copies the number
 * and says to dial it from a phone.
 */

import React, { useState } from 'react';
import { Phone } from 'lucide-react';

export function telHref(phone) {
  if (!phone) return null;
  const digits = String(phone).replace(/[^\d+]/g, '');
  if (!digits || digits === '+') return null;
  return `tel:${digits}`;
}

function isTouchScreen() {
  try {
    return window.matchMedia('(pointer: coarse)').matches;
  } catch {
    return false;
  }
}

export default function CallNumber({ phone, label, className = '', iconSize = 14 }) {
  const [touch] = useState(isTouchScreen);
  const [copied, setCopied] = useState(false);
  const href = telHref(phone);
  if (!href) return null;
  const shown = String(phone).trim();
  const text = label ?? `Call ${shown}`;

  if (touch) {
    return (
      <a href={href} className={className}>
        <Phone size={iconSize} /> {text}
      </a>
    );
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(shown);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 2500);
    } catch {
      setCopied(false);
    }
  };
  return (
    <span className="inline-flex flex-col items-center gap-1">
      <button type="button" onClick={copy} className={className}>
        <Phone size={iconSize} /> {copied ? `${shown} copied` : `Copy ${shown}`}
      </button>
      <span className="t-meta text-text-faint">Dial {shown} from your phone</span>
    </span>
  );
}
