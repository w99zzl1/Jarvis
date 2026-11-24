import { useEffect, useRef } from "react";

/**
 * Cinematic J.A.R.V.I.S. core from the user-provided v5.3 design.
 *
 * The original sketch opens its own microphone. Jarvis already owns the mic
 * session for streaming STT, so this component only consumes the AnalyserNode
 * exposed by `audioCapture` and keeps the idle animation alive when no audio
 * is being captured.
 */

interface JarvisCoreVisualizerProps {
  analyser: AnalyserNode | null;
  active: boolean;
}

// The original sketch drew a 460px-wide figure inside a 700px canvas, leaving
// ~120px of dead space on every side. Crop the canvas to the drawing itself so
// the wrapper only contains the core plus a tiny safety margin.
const SIZE = 480;
const CENTER = SIZE / 2;
const CURRENT_PALETTE = {
  accent: "81, 219, 255",
  code: "140, 230, 255",
  label: "160, 243, 255",
  inner: "2, 12, 28",
} as const;
const ONYX_PALETTE = {
  accent: "16, 185, 129",
  code: "110, 231, 183",
  label: "167, 243, 208",
  inner: "2, 18, 13",
} as const;
type CorePalette = typeof CURRENT_PALETTE;
let ACTIVE_PALETTE: CorePalette = CURRENT_PALETTE;
// Never render below the original 480px buffer, but go higher on HiDPI and in
// voice mode (the wrapper is scaled by 1.48 there) so the vectors stay crisp.
const OVERSAMPLE = 1.5;

function drawArcOn(
  context: CanvasRenderingContext2D,
  radius: number,
  start: number,
  end: number,
  width = 1,
  alpha = 0.5,
  color: string = ACTIVE_PALETTE.accent,
) {
  context.beginPath();
  context.arc(0, 0, radius, start, end);
  context.lineWidth = width;
  context.strokeStyle = `rgba(${color}, ${alpha})`;
  context.setLineDash([]);
  context.stroke();
}

export default function JarvisCoreVisualizer({ analyser, active }: JarvisCoreVisualizerProps) {
  const coreRef = useRef<HTMLCanvasElement>(null);
  const bloomRef = useRef<HTMLCanvasElement>(null);
  const stateRef = useRef({ analyser, active });
  stateRef.current = { analyser, active };

  useEffect(() => {
    const core = coreRef.current;
    const bloom = bloomRef.current;
    if (!core || !bloom) return;
    const ctx = core.getContext("2d");
    const bloomCtx = bloom.getContext("2d");
    if (!ctx || !bloomCtx) return;

    let lastCss = 0;
    let lastDpr = 0;
    const syncResolution = () => {
      const css = core.clientWidth || SIZE;
      const dpr = Math.min(window.devicePixelRatio || 1, 2);
      if (css === lastCss && dpr === lastDpr) return;
      lastCss = css;
      lastDpr = dpr;
      const pixels = Math.min(1024, Math.max(SIZE, Math.round(css * dpr * OVERSAMPLE)));
      core.width = pixels;
      core.height = pixels;
      bloom.width = pixels;
      bloom.height = pixels;
      const scale = pixels / SIZE;
      ctx.setTransform(scale, 0, 0, scale, 0, 0);
      bloomCtx.setTransform(scale, 0, 0, scale, 0, 0);
    };
    syncResolution();
    const resizeObserver = new ResizeObserver(syncResolution);
    resizeObserver.observe(core);
    window.addEventListener("resize", syncResolution);

    let frame = 0;
    let rotation1 = 0;
    let rotation2 = 0;
    let frequencyData: Uint8Array | null = null;
    let timeData: Uint8Array | null = null;

    const particles = Array.from({ length: 80 }, () => ({
      radius: 70 + Math.random() * 100,
      angle: Math.random() * Math.PI * 2,
      speed: (Math.random() * 0.004 + 0.002) * (Math.random() > 0.5 ? 1 : -1),
      size: Math.random() * 1.8 + 0.8,
    }));
    const pulses: { radius: number; alpha: number; maxRadius: number }[] = [];

    const codeRingRadius = 195;
    const codeChars = "01 10 11 00 ".repeat(10).trim().split("");
    const codeStep = (Math.PI * 2) / codeChars.length;
    const codePositions = codeChars.map((char, i) => {
      const angle = i * codeStep;
      return { char, x: Math.cos(angle) * codeRingRadius, y: Math.sin(angle) * codeRingRadius };
    });

    const draw = () => {
      frame = requestAnimationFrame(draw);
      ACTIVE_PALETTE =
        document.documentElement.dataset.theme === "onyx" ? ONYX_PALETTE : CURRENT_PALETTE;
      ctx.clearRect(0, 0, SIZE, SIZE);
      bloomCtx.clearRect(0, 0, SIZE, SIZE);

      let volume = 0;
      let bass = 0;
      let mid = 0;
      let treble = 0;
      const currentAnalyser = stateRef.current.analyser;

      if (currentAnalyser) {
        if (!frequencyData || frequencyData.length !== currentAnalyser.frequencyBinCount) {
          frequencyData = new Uint8Array(currentAnalyser.frequencyBinCount);
        }
        if (!timeData || timeData.length !== currentAnalyser.fftSize) {
          timeData = new Uint8Array(currentAnalyser.fftSize);
        }
        currentAnalyser.getByteFrequencyData(frequencyData);
        currentAnalyser.getByteTimeDomainData(timeData);
        for (let i = 0; i < frequencyData.length; i++) {
          volume += frequencyData[i];
          if (i < 6) bass += frequencyData[i];
          else if (i < 20) mid += frequencyData[i];
          else treble += frequencyData[i];
        }
        volume /= frequencyData.length;
        bass /= 6;
        mid /= 14;
        treble /= Math.max(1, frequencyData.length - 20);
      } else if (stateRef.current.active) {
        // Speaking/TTS playback is not fed back into the microphone. Keep a
        // gentle synthetic breath so the core still feels alive while visible.
        const t = performance.now() / 1000;
        volume = 40 + Math.sin(t * 2.2) * 8;
        bass = 36 + Math.sin(t * 1.4) * 10;
        mid = 28 + Math.sin(t * 3.1) * 6;
        treble = 22 + Math.sin(t * 4.3) * 5;
      }

      const logVolume = (volume / 255) * 120 / (1 + (volume / 255) * 0.8);
      const logBass = (bass / 255) * 90 / (1 + (bass / 255) * 0.7);
      const logMid = (mid / 255) * 80 / (1 + (mid / 255) * 0.7);
      const logTreble = (treble / 255) * 70 / (1 + (treble / 255) * 0.6);

      rotation1 += 0.004 + logVolume * 0.001;
      rotation2 -= 0.006 + logMid * 0.0008;

      // ---------------- main canvas ----------------
      ctx.save();
      ctx.translate(CENTER, CENTER);

      for (let r = 40; r <= 230; r += 12) {
        ctx.beginPath();
        ctx.arc(0, 0, r, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.035)`;
        ctx.lineWidth = 0.8;
        ctx.stroke();
      }
      for (const r of [80, 120, 165, 195, 225]) {
        ctx.beginPath();
        ctx.arc(0, 0, r, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.07)`;
        ctx.lineWidth = 1;
        ctx.stroke();
      }
      for (let a = 0; a < Math.PI * 2; a += Math.PI / 6) {
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * 40, Math.sin(a) * 40);
        ctx.lineTo(Math.cos(a) * 230, Math.sin(a) * 230);
        ctx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.04)`;
        ctx.lineWidth = 0.55;
        ctx.stroke();
      }

      particles.forEach((p) => {
        p.angle += p.speed + logVolume * 0.0005;
        const x = Math.cos(p.angle) * (p.radius + logMid * 0.1);
        const y = Math.sin(p.angle) * (p.radius + logMid * 0.1);
        ctx.fillStyle = `rgba(${ACTIVE_PALETTE.accent}, ${0.25 + logVolume / 150})`;
        ctx.beginPath();
        ctx.arc(x, y, p.size, 0, Math.PI * 2);
        ctx.fill();
      });

      ctx.save();
      ctx.rotate(rotation1 * 0.25);
      ctx.font = "bold 10px monospace";
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      codePositions.forEach((p) => {
        ctx.fillStyle = `rgba(${ACTIVE_PALETTE.code}, ${0.35 + logVolume / 200})`;
        ctx.fillText(p.char, p.x, p.y);
      });
      ctx.restore();

      ctx.save();
      ctx.rotate(rotation1 * 0.4);
      const r1 = 175 + logTreble;
      drawArcOn(ctx, r1, 0, Math.PI * 0.4, 3.5, 0.65);
      drawArcOn(ctx, r1, Math.PI * 0.5, Math.PI * 1.1, 2.8, 0.35);
      drawArcOn(ctx, r1, Math.PI * 1.2, Math.PI * 1.7, 2.8, 0.55);
      drawArcOn(ctx, r1, Math.PI * 1.8, Math.PI * 1.95, 4.5, 0.85);
      ctx.restore();

      ctx.save();
      ctx.rotate(rotation2);
      const r2 = 155 + logMid * 0.5;
      drawArcOn(ctx, r2, 0, Math.PI * 0.7, 2.5, 0.45);
      drawArcOn(ctx, r2, Math.PI * 0.8, Math.PI * 1.8, 2, 0.35);
      drawArcOn(ctx, r2 - 3, Math.PI * 0.3, Math.PI * 0.6, 1.5, 0.25);
      drawArcOn(ctx, r2 - 3, Math.PI * 1.0, Math.PI * 1.5, 1.5, 0.25);
      ctx.restore();

      ctx.save();
      ctx.rotate(rotation1 * 1.3);
      const r3 = 135;
      const blocks = 72;
      for (let i = 0; i < blocks; i++) {
        if ((i > 4 && i < 10) || (i > 32 && i < 38) || (i > 54 && i < 58)) continue;
        const angle = (i * Math.PI * 2) / blocks;
        const noise = Math.sin(i * 0.4 + rotation1 * 6) * 3;
        const blockHeight = 6 + logBass * 0.4 + noise;
        ctx.save();
        ctx.rotate(angle);
        ctx.fillStyle = i % 6 === 0 ? `rgba(${ACTIVE_PALETTE.accent}, 0.85)` : `rgba(${ACTIVE_PALETTE.accent}, 0.45)`;
        ctx.fillRect(-1.8, -r3, 3.6, -blockHeight);
        ctx.restore();
      }
      ctx.restore();

      ctx.save();
      ctx.rotate(-rotation1 * 0.7);
      ctx.beginPath();
      ctx.arc(0, 0, 110, 0, Math.PI * 2);
      ctx.setLineDash([2, 4]);
      ctx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.35)`;
      ctx.lineWidth = 1.8;
      ctx.stroke();
      ctx.setLineDash([]);
      drawArcOn(ctx, 115, 0, Math.PI * 0.4, 3, 0.55);
      drawArcOn(ctx, 115, Math.PI * 0.9, Math.PI * 1.3, 3, 0.55);
      drawArcOn(ctx, 108, Math.PI * 1.5, Math.PI * 1.8, 2, 0.3);
      ctx.restore();

      ctx.save();
      ctx.rotate(rotation2 * 0.4);
      const r5 = 90 - logVolume * 0.1;
      drawArcOn(ctx, r5, 0, Math.PI * 0.6, 3.5, 0.75);
      drawArcOn(ctx, r5, Math.PI * 0.7, Math.PI * 1.3, 2, 0.35);
      drawArcOn(ctx, r5, Math.PI * 1.4, Math.PI * 1.9, 2.5, 0.55);
      ctx.restore();

      const coreRadius = 70 + logVolume * 0.2;
      const grad = ctx.createRadialGradient(0, 0, coreRadius * 0.3, 0, 0, coreRadius);
      grad.addColorStop(0, `rgba(${ACTIVE_PALETTE.inner}, 0)`);
      grad.addColorStop(0.6, `rgba(${ACTIVE_PALETTE.accent}, ${0.04 + logVolume / 250})`);
      grad.addColorStop(0.85, `rgba(${ACTIVE_PALETTE.accent}, ${0.2 + logVolume / 150})`);
      grad.addColorStop(0.95, `rgba(${ACTIVE_PALETTE.accent}, ${0.5 + logVolume / 80})`);
      grad.addColorStop(1, `rgba(${ACTIVE_PALETTE.accent}, ${0.75 + logVolume / 50})`);
      ctx.beginPath();
      ctx.arc(0, 0, coreRadius, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();
      drawArcOn(ctx, coreRadius, 0, Math.PI * 2, 4.5, 0.8 + logVolume * 0.004);

      if (volume > 80 && pulses.length < 6) {
        pulses.push({ radius: coreRadius + 5, alpha: 0.8, maxRadius: 180 });
      }
      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i];
        p.radius += 3.5;
        p.alpha -= 0.018;
        if (p.alpha <= 0 || p.radius > p.maxRadius) {
          pulses.splice(i, 1);
          continue;
        }
        ctx.beginPath();
        ctx.arc(0, 0, p.radius, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, ${p.alpha})`;
        ctx.lineWidth = 3.5;
        ctx.stroke();
      }

      if (timeData) {
        const waveRadius = 185;
        const step = (Math.PI * 2) / timeData.length;
        ctx.beginPath();
        for (let i = 0; i < timeData.length; i++) {
          const v = timeData[i] / 128 - 1;
          const angle = i * step + rotation1 * 0.2;
          const r = waveRadius + v * 12;
          const x = Math.cos(angle) * r;
          const y = Math.sin(angle) * r;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.closePath();
        ctx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.5)`;
        ctx.lineWidth = 3;
        ctx.stroke();

        ctx.beginPath();
        for (let i = 0; i < timeData.length; i++) {
          const v = timeData[i] / 128 - 1;
          const angle = i * step + rotation1 * 0.2 + 0.1;
          const r = waveRadius + v * 9;
          const x = Math.cos(angle) * r;
          const y = Math.sin(angle) * r;
          if (i === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.25)`;
        ctx.lineWidth = 1.5;
        ctx.stroke();
      }

      const activeNow = stateRef.current.active;
      ctx.fillStyle = `rgba(${ACTIVE_PALETTE.label}, ${0.85 + logVolume / 150})`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      // Label sizes are tuned for the cropped 480px canvas so it stays
      // readable against the enlarged core.
      // Same technical stack as the HUD captions, so the core label and the
      // header/message metadata read as one system instead of two typefaces.
      const hudFont = '"JetBrains Mono", "Fira Code", "SFMono-Regular", Consolas, monospace';
      ctx.font = `bold ${activeNow ? 52 : 30}px ${hudFont}`;
      ctx.fillText(activeNow ? "J.A.R.V.I.S." : "CLICK TO SYNC", 0, 0);
      ctx.restore();

      // ---------------- bloom canvas ----------------
      bloomCtx.save();
      bloomCtx.translate(CENTER, CENTER);

      const bGrad = bloomCtx.createRadialGradient(0, 0, coreRadius * 0.3, 0, 0, coreRadius);
      bGrad.addColorStop(0, `rgba(${ACTIVE_PALETTE.accent}, 0)`);
      bGrad.addColorStop(0.6, `rgba(${ACTIVE_PALETTE.accent}, ${0.3 + logVolume / 150})`);
      bGrad.addColorStop(0.85, `rgba(${ACTIVE_PALETTE.accent}, ${0.6 + logVolume / 80})`);
      bGrad.addColorStop(0.95, `rgba(${ACTIVE_PALETTE.accent}, ${0.9 + logVolume / 40})`);
      bGrad.addColorStop(1, `rgba(${ACTIVE_PALETTE.accent}, 1)`);
      bloomCtx.beginPath();
      bloomCtx.arc(0, 0, coreRadius, 0, Math.PI * 2);
      bloomCtx.fillStyle = bGrad;
      bloomCtx.fill();
      drawArcOn(bloomCtx, coreRadius, 0, Math.PI * 2, 5, 1);

      bloomCtx.save();
      bloomCtx.rotate(rotation1 * 0.4);
      drawArcOn(bloomCtx, 175 + logTreble, 0, Math.PI * 0.4, 4, 0.9);
      drawArcOn(bloomCtx, 175 + logTreble, Math.PI * 0.5, Math.PI * 1.1, 3, 0.6);
      drawArcOn(bloomCtx, 175 + logTreble, Math.PI * 1.2, Math.PI * 1.7, 3, 0.7);
      drawArcOn(bloomCtx, 175 + logTreble, Math.PI * 1.8, Math.PI * 1.95, 5, 1);
      bloomCtx.restore();

      bloomCtx.save();
      bloomCtx.rotate(rotation2);
      const r2b = 155 + logMid * 0.5;
      drawArcOn(bloomCtx, r2b, 0, Math.PI * 0.7, 3, 0.7);
      drawArcOn(bloomCtx, r2b, Math.PI * 0.8, Math.PI * 1.8, 2.5, 0.5);
      drawArcOn(bloomCtx, r2b - 3, Math.PI * 0.3, Math.PI * 0.6, 2, 0.4);
      drawArcOn(bloomCtx, r2b - 3, Math.PI * 1.0, Math.PI * 1.5, 2, 0.4);
      bloomCtx.restore();

      bloomCtx.save();
      bloomCtx.rotate(rotation1 * 1.3);
      for (let i = 0; i < blocks; i++) {
        if ((i > 4 && i < 10) || (i > 32 && i < 38) || (i > 54 && i < 58)) continue;
        const angle = (i * Math.PI * 2) / blocks;
        const noise = Math.sin(i * 0.4 + rotation1 * 6) * 3;
        const blockHeight = 6 + logBass * 0.4 + noise;
        bloomCtx.save();
        bloomCtx.rotate(angle);
        bloomCtx.fillStyle = i % 6 === 0 ? `rgba(${ACTIVE_PALETTE.accent}, 0.95)` : `rgba(${ACTIVE_PALETTE.accent}, 0.7)`;
        bloomCtx.fillRect(-2, -r3, 4, -blockHeight);
        bloomCtx.restore();
      }
      bloomCtx.restore();

      bloomCtx.save();
      bloomCtx.rotate(-rotation1 * 0.7);
      bloomCtx.beginPath();
      bloomCtx.arc(0, 0, 110, 0, Math.PI * 2);
      bloomCtx.setLineDash([2, 4]);
      bloomCtx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.5)`;
      bloomCtx.lineWidth = 2;
      bloomCtx.stroke();
      bloomCtx.setLineDash([]);
      drawArcOn(bloomCtx, 115, 0, Math.PI * 0.4, 3.5, 0.8);
      drawArcOn(bloomCtx, 115, Math.PI * 0.9, Math.PI * 1.3, 3.5, 0.8);
      drawArcOn(bloomCtx, 108, Math.PI * 1.5, Math.PI * 1.8, 2.5, 0.5);
      bloomCtx.restore();

      bloomCtx.save();
      bloomCtx.rotate(rotation2 * 0.4);
      const r5b = 90 - logVolume * 0.1;
      drawArcOn(bloomCtx, r5b, 0, Math.PI * 0.6, 4, 0.9);
      drawArcOn(bloomCtx, r5b, Math.PI * 0.7, Math.PI * 1.3, 2.5, 0.5);
      drawArcOn(bloomCtx, r5b, Math.PI * 1.4, Math.PI * 1.9, 3, 0.7);
      bloomCtx.restore();

      pulses.forEach((p) => {
        bloomCtx.beginPath();
        bloomCtx.arc(0, 0, p.radius, 0, Math.PI * 2);
        bloomCtx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, ${p.alpha * 1.5})`;
        bloomCtx.lineWidth = 5;
        bloomCtx.stroke();
      });

      if (timeData) {
        const waveRadius = 185;
        const step = (Math.PI * 2) / timeData.length;
        bloomCtx.beginPath();
        for (let i = 0; i < timeData.length; i++) {
          const v = timeData[i] / 128 - 1;
          const angle = i * step + rotation1 * 0.2;
          const r = waveRadius + v * 12;
          const x = Math.cos(angle) * r;
          const y = Math.sin(angle) * r;
          if (i === 0) bloomCtx.moveTo(x, y);
          else bloomCtx.lineTo(x, y);
        }
        bloomCtx.closePath();
        bloomCtx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.7)`;
        bloomCtx.lineWidth = 4;
        bloomCtx.stroke();

        bloomCtx.beginPath();
        for (let i = 0; i < timeData.length; i++) {
          const v = timeData[i] / 128 - 1;
          const angle = i * step + rotation1 * 0.2 + 0.1;
          const r = waveRadius + v * 9;
          const x = Math.cos(angle) * r;
          const y = Math.sin(angle) * r;
          if (i === 0) bloomCtx.moveTo(x, y);
          else bloomCtx.lineTo(x, y);
        }
        bloomCtx.strokeStyle = `rgba(${ACTIVE_PALETTE.accent}, 0.4)`;
        bloomCtx.lineWidth = 2;
        bloomCtx.stroke();
      }

      bloomCtx.restore();
    };

    draw();
    return () => {
      cancelAnimationFrame(frame);
      resizeObserver.disconnect();
      window.removeEventListener("resize", syncResolution);
    };
  }, []);

  return (
    <div className="jarvis-core-stack pointer-events-none absolute inset-0">
      <canvas id="jarvisCore" ref={coreRef} width={SIZE} height={SIZE} />
      <canvas id="bloomLayer" ref={bloomRef} width={SIZE} height={SIZE} />
    </div>
  );
}
