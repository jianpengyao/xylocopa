import { useState, useEffect } from "react";
import { fluentFlatUrl } from "../lib/projectEmoji";
import { rem } from "../lib/textScale";

/**
 * Render an emoji using Microsoft Fluent UI Emoji (Flat) from jsdelivr CDN,
 * falling back to the system emoji font if the CDN asset isn't available.
 */
export default function FluentEmoji({ char, size = 18, className = "", title }) {
  const url = fluentFlatUrl(char);
  const [errored, setErrored] = useState(false);

  // Reset error flag when char changes so a new valid emoji gets another try.
  useEffect(() => { setErrored(false); }, [char]);

  if (!char) return null;
  // `size` is in design px; render it in rem so it follows Settings > Text size.
  const dim = rem(size);

  if (!url || errored) {
    return (
      <span
        className={className}
        style={{ fontSize: dim, lineHeight: 1, display: "inline-block" }}
        title={title || char}
      >
        {char}
      </span>
    );
  }

  return (
    <img
      src={url}
      alt={char}
      width={size}
      height={size}
      className={className}
      style={{ display: "inline-block", width: dim, height: dim }}
      draggable={false}
      onError={() => setErrored(true)}
      title={title || char}
    />
  );
}
