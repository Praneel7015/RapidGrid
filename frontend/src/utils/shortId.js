/**
 * A short, readable form of an incident id for screens: INC-<first 6 hex,
 * upper case> (INC-9295FA). The full id stays in the title attribute and
 * in every API call; this is display only.
 */
export default function shortId(id) {
  if (typeof id !== 'string') return id ?? '';
  const m = /^INC-([0-9a-f]{6})[0-9a-f]*$/i.exec(id);
  return m ? `INC-${m[1].toUpperCase()}` : id;
}
