/**
 * Click-to-call via tel: links. Strips formatting for the href; keeps the
 * display label human-readable.
 */

import React from 'react';
import { Phone } from 'lucide-react';

export function toTelHref(phone) {
  if (!phone) return null;
  const digits = String(phone).replace(/[^\d+]/g, '');
  if (!digits || digits === '+') return null;
  return `tel:${digits}`;
}

export default function CallButton({
  phone,
  label = 'Call',
  variant = 'quiet',
  className = '',
  size = 'sm',
}) {
  const href = toTelHref(phone);
  if (!href) return null;

  const variants = {
    quiet: 'border-ink bg-paper text-text hover:bg-ink hover:text-on-ink',
    primary: 'border-ink bg-signal text-ink hover:bg-ink hover:text-signal',
    ink: 'border-rule-ink bg-transparent text-on-ink hover:border-signal hover:text-signal',
  };
  const sizes = {
    sm: 'px-3 py-2 text-[11px] lg:py-1.5',
    md: 'px-4 py-2.5 text-[12px]',
  };

  return (
    <a
      href={href}
      className={`tap inline-flex items-center justify-center gap-1.5 rounded-sm border font-mono font-bold uppercase tracking-[0.08em] transition-colors ${variants[variant]} ${sizes[size]} ${className}`}
    >
      <Phone size={13} />
      {label}
    </a>
  );
}
