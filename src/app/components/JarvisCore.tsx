import { useEffect, useRef, useCallback, useState } from "react";

export default function JarvisCore() {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const bloomRef = useRef<HTMLCanvasElement>(null);
  const [audioReady, setAudioReady] = useState(false);

  const audioRef = useRef<{
    ctx: AudioContext;
    analyser: AnalyserNode;
    dataArray: Uint8Array;
    timeDataArray: Uint8Array;
  } | null>(null);

  const animRef = useRef<number>(0);
  const rotation1Ref = useRef(0);
  const rotation2Ref = useRef(0);
  const particlesRef = useRef(
    Array.from({ length: 80 }, () => ({
      radius: 105 + Math.random() * 150,
      angle: Math.random() * Math.PI * 2,
      speed: (Math.random() * 0.004 + 0.002) * (Math.random() > 0.5 ? 1 : -1),
      size: Math.random() * 2.5 + 1.2,
    }))
  );
  const pulsesRef = useRef<{ radius: number; alpha: number; maxRadius: number }[]>([]);

  const initAudio = useCallback(async () => {
    if (audioRef.current) return;
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const ctx = new (window.AudioContext || (window as any).webkitAudioContext)();
      const analyser = ctx.createAnalyser();
      const source = ctx.createMediaStreamSource(stream);
      source.connect(analyser);
      analyser.fftSize = 256;
      const dataArray = new Uint8Array(analyser.frequencyBinCount);
      const timeDataArray = new Uint8Array(analyser.fftSize);
      audioRef.current = { ctx, analyser, dataArray, timeDataArray };
      setAudioReady(true);
    } catch {
      // fallback — no mic
    }
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    const bloom = bloomRef.current;
    if (!canvas || !bloom) return;
    const ctx = canvas.getContext("2d")!;
    const bloomCtx = bloom.getContext("2d")!;

    const W = 700;
    const H = 700;
    const CX = W / 2;
    const CY = H / 2;
    canvas.width = W;
    canvas.height = H;
    bloom.width = W;
    bloom.height = H;

    const codeRingRadius = 293;
    const codeChars = "01 10 11 00 ".repeat(10).trim().split("");
    const codeStep = (Math.PI * 2) / codeChars.length;
    const codePositions = codeChars.map((ch, i) => {
      const angle = i * codeStep;
      return { char: ch, x: Math.cos(angle) * codeRingRadius, y: Math.sin(angle) * codeRingRadius, angle };
    });

    function drawArc(
      c: CanvasRenderingContext2D,
      radius: number,
      start: number,
      end: number,
      width = 1,
      alpha = 0.5,
      color = "81, 219, 255"
    ) {
      c.beginPath();
      c.arc(0, 0, radius, start, end);
      c.lineWidth = width;
      c.strokeStyle = `rgba(${color}, ${alpha})`;
      c.setLineDash([]);
      c.stroke();
    }

    let blocks = 72;
    let r3 = 203;

    function frame() {
      animRef.current = requestAnimationFrame(frame);
      ctx.clearRect(0, 0, W, H);
      bloomCtx.clearRect(0, 0, W, H);

      let volume = 0, bass = 0, mid = 0, treble = 0;
      const audio = audioRef.current;
      if (audio) {
        audio.analyser.getByteFrequencyData(audio.dataArray);
        audio.analyser.getByteTimeDomainData(audio.timeDataArray);
        const d = audio.dataArray;
        for (let i = 0; i < d.length; i++) {
          volume += d[i];
          if (i < 6) bass += d[i];
          else if (i < 20) mid += d[i];
          else treble += d[i];
        }
        volume /= d.length;
        bass /= 6;
        mid /= 14;
        treble /= d.length - 20;
      }

      const logVol = (volume / 255) * 120 / (1 + (volume / 255) * 0.8);
      const logBass = (bass / 255) * 90 / (1 + (bass / 255) * 0.7);
      const logMid = (mid / 255) * 80 / (1 + (mid / 255) * 0.7);
      const logTreble = (treble / 255) * 70 / (1 + (treble / 255) * 0.6);

      rotation1Ref.current += 0.004 + (logVol * 0.001);
      rotation2Ref.current -= 0.006 + (logMid * 0.0008);

      const r1 = rotation1Ref.current;
      const r2 = rotation2Ref.current;
      const particles = particlesRef.current;
      const pulses = pulsesRef.current;

      // ---- MAIN CANVAS ----
      ctx.save();
      ctx.translate(CX, CY);

      for (let r = 60; r <= 345; r += 18) {
        ctx.beginPath();
        ctx.arc(0, 0, r, 0, Math.PI * 2);
        ctx.strokeStyle = "rgba(81, 219, 255, 0.025)";
        ctx.lineWidth = 0.5;
        ctx.stroke();
      }
      for (const r of [120, 180, 248, 293, 338]) {
        ctx.beginPath();
        ctx.arc(0, 0, r, 0, Math.PI * 2);
        ctx.strokeStyle = "rgba(81, 219, 255, 0.06)";
        ctx.lineWidth = 0.7;
        ctx.stroke();
      }
      for (let a = 0; a < Math.PI * 2; a += Math.PI / 6) {
        ctx.beginPath();
        ctx.moveTo(Math.cos(a) * 60, Math.sin(a) * 60);
        ctx.lineTo(Math.cos(a) * 345, Math.sin(a) * 345);
        ctx.strokeStyle = "rgba(81, 219, 255, 0.03)";
        ctx.lineWidth = 0.3;
        ctx.stroke();
      }

      for (const p of particles) {
        p.angle += p.speed + (logVol * 0.0005);
        const px = Math.cos(p.angle) * (p.radius + logMid * 0.15);
        const py = Math.sin(p.angle) * (p.radius + logMid * 0.15);
        ctx.fillStyle = `rgba(81, 219, 255, ${0.25 + (logVol / 150)})`;
        ctx.beginPath();
        ctx.arc(px, py, p.size, 0, Math.PI * 2);
        ctx.fill();
      }

      ctx.save();
      ctx.rotate(r1 * 0.25);
      ctx.font = 'bold 14px "Space Mono", monospace';
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      for (const p of codePositions) {
        const alpha = 0.35 + (logVol / 200);
        ctx.fillStyle = `rgba(140, 230, 255, ${alpha})`;
        ctx.fillText(p.char, p.x, p.y);
      }
      ctx.restore();

      ctx.save();
      ctx.rotate(r1 * 0.4);
      const arcR1 = 263 + logTreble * 1.5;
      drawArc(ctx, arcR1, 0, Math.PI * 0.4, 5, 0.65);
      drawArc(ctx, arcR1, Math.PI * 0.5, Math.PI * 1.1, 4, 0.35);
      drawArc(ctx, arcR1, Math.PI * 1.2, Math.PI * 1.7, 4, 0.55);
      drawArc(ctx, arcR1, Math.PI * 1.8, Math.PI * 1.95, 6.5, 0.85);
      ctx.restore();

      ctx.save();
      ctx.rotate(r2);
      const arcR2 = 233 + logMid * 0.75;
      drawArc(ctx, arcR2, 0, Math.PI * 0.7, 3.5, 0.45);
      drawArc(ctx, arcR2, Math.PI * 0.8, Math.PI * 1.8, 3, 0.35);
      drawArc(ctx, arcR2 - 4, Math.PI * 0.3, Math.PI * 0.6, 2.2, 0.25);
      drawArc(ctx, arcR2 - 4, Math.PI * 1.0, Math.PI * 1.5, 2.2, 0.25);
      ctx.restore();

      ctx.save();
      ctx.rotate(r1 * 1.3);
      for (let i = 0; i < blocks; i++) {
        const angle = (i * Math.PI * 2) / blocks;
        const noise = Math.sin(i * 0.4 + r1 * 6) * 4.5;
        const bh = 9 + logBass * 0.6 + noise;
        if ((i > 4 && i < 10) || (i > 32 && i < 38) || (i > 54 && i < 58)) continue;
        ctx.save();
        ctx.rotate(angle);
        ctx.fillStyle = i % 6 === 0 ? "rgba(81, 219, 255, 0.85)" : "rgba(81, 219, 255, 0.45)";
        ctx.fillRect(-2.7, -r3, 5.4, -bh);
        ctx.restore();
      }
      ctx.restore();

      ctx.save();
      ctx.rotate(-r1 * 0.7);
      const r4 = 165;
      ctx.beginPath();
      ctx.arc(0, 0, r4, 0, Math.PI * 2);
      ctx.setLineDash([3, 6]);
      ctx.strokeStyle = "rgba(81, 219, 255, 0.35)";
      ctx.lineWidth = 2.7;
      ctx.stroke();
      ctx.setLineDash([]);
      drawArc(ctx, r4 + 7, Math.PI * 0.0, Math.PI * 0.4, 4.5, 0.55);
      drawArc(ctx, r4 + 7, Math.PI * 0.9, Math.PI * 1.3, 4.5, 0.55);
      drawArc(ctx, r4 - 3, Math.PI * 1.5, Math.PI * 1.8, 3, 0.3);
      ctx.restore();

      ctx.save();
      ctx.rotate(r2 * 0.4);
      const r5 = 135 - logVol * 0.15;
      drawArc(ctx, r5, 0, Math.PI * 0.6, 5, 0.75);
      drawArc(ctx, r5, Math.PI * 0.7, Math.PI * 1.3, 3, 0.35);
      drawArc(ctx, r5, Math.PI * 1.4, Math.PI * 1.9, 3.5, 0.55);
      ctx.restore();

      const coreRadius = 105 + logVol * 0.3;
      const grad = ctx.createRadialGradient(0, 0, coreRadius * 0.3, 0, 0, coreRadius);
      grad.addColorStop(0, "rgba(2, 12, 28, 0)");
      grad.addColorStop(0.6, `rgba(81, 219, 255, ${0.04 + (logVol / 250)})`);
      grad.addColorStop(0.85, `rgba(81, 219, 255, ${0.2 + (logVol / 150)})`);
      grad.addColorStop(0.95, `rgba(81, 219, 255, ${0.5 + (logVol / 80)})`);
      grad.addColorStop(1, `rgba(81, 219, 255, ${0.75 + (logVol / 50)})`);
      ctx.beginPath();
      ctx.arc(0, 0, coreRadius, 0, Math.PI * 2);
      ctx.fillStyle = grad;
      ctx.fill();
      drawArc(ctx, coreRadius, 0, Math.PI * 2, 6, 0.8 + logVol * 0.004);

      if (volume > 80 && pulses.length < 6) {
        pulses.push({ radius: coreRadius + 7, alpha: 0.8, maxRadius: 270 });
      }
      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i];
        p.radius += 5;
        p.alpha -= 0.018;
        if (p.alpha <= 0 || p.radius > p.maxRadius) {
          pulses.splice(i, 1);
          continue;
        }
        ctx.beginPath();
        ctx.arc(0, 0, p.radius, 0, Math.PI * 2);
        ctx.strokeStyle = `rgba(81, 219, 255, ${p.alpha})`;
        ctx.lineWidth = 5;
        ctx.stroke();
      }

      if (audio && audio.timeDataArray) {
        const waveRadius = 278;
        const step = (Math.PI * 2) / audio.timeDataArray.length;
        ctx.beginPath();
        for (let i = 0; i < audio.timeDataArray.length; i++) {
          const v = audio.timeDataArray[i] / 128.0 - 1.0;
          const a = i * step + r1 * 0.2;
          const wr = waveRadius + v * 18;
          const wx = Math.cos(a) * wr;
          const wy = Math.sin(a) * wr;
          if (i === 0) ctx.moveTo(wx, wy);
          else ctx.lineTo(wx, wy);
        }
        ctx.closePath();
        ctx.strokeStyle = "rgba(81, 219, 255, 0.5)";
        ctx.lineWidth = 4.5;
        ctx.stroke();

        ctx.beginPath();
        for (let i = 0; i < audio.timeDataArray.length; i++) {
          const v = audio.timeDataArray[i] / 128.0 - 1.0;
          const a = i * step + r1 * 0.2 + 0.1;
          const wr = waveRadius + v * 13;
          const wx = Math.cos(a) * wr;
          const wy = Math.sin(a) * wr;
          if (i === 0) ctx.moveTo(wx, wy);
          else ctx.lineTo(wx, wy);
        }
        ctx.strokeStyle = "rgba(81, 219, 255, 0.25)";
        ctx.lineWidth = 2.2;
        ctx.stroke();
      }

      ctx.fillStyle = `rgba(160, 243, 255, ${0.85 + (logVol / 150)})`;
      ctx.textAlign = "center";
      ctx.textBaseline = "middle";
      if (audio) {
        ctx.font = 'bold 42px "Orbitron", sans-serif';
        ctx.fillText("J.A.R.V.I.S.", 0, 0);
      } else {
        ctx.font = 'bold 21px "Orbitron", sans-serif';
        ctx.fillText("CLICK TO SYNC", 0, 0);
      }

      ctx.restore();

      // ---- BLOOM CANVAS ----
      bloomCtx.save();
      bloomCtx.translate(CX, CY);

      const bGrad = bloomCtx.createRadialGradient(0, 0, coreRadius * 0.3, 0, 0, coreRadius);
      bGrad.addColorStop(0, "rgba(81, 219, 255, 0)");
      bGrad.addColorStop(0.6, `rgba(81, 219, 255, ${0.3 + (logVol / 150)})`);
      bGrad.addColorStop(0.85, `rgba(81, 219, 255, ${0.6 + (logVol / 80)})`);
      bGrad.addColorStop(0.95, `rgba(81, 219, 255, ${0.9 + (logVol / 40)})`);
      bGrad.addColorStop(1, "rgba(81, 219, 255, 1.0)");
      bloomCtx.beginPath();
      bloomCtx.arc(0, 0, coreRadius, 0, Math.PI * 2);
      bloomCtx.fillStyle = bGrad;
      bloomCtx.fill();
      drawArc(bloomCtx, coreRadius, 0, Math.PI * 2, 7, 1.0, "81, 219, 255");

      bloomCtx.save();
      bloomCtx.rotate(r1 * 0.4);
      drawArc(bloomCtx, arcR1, 0, Math.PI * 0.4, 6, 0.9, "81, 219, 255");
      drawArc(bloomCtx, arcR1, Math.PI * 0.5, Math.PI * 1.1, 4.5, 0.6, "81, 219, 255");
      drawArc(bloomCtx, arcR1, Math.PI * 1.2, Math.PI * 1.7, 4.5, 0.7, "81, 219, 255");
      drawArc(bloomCtx, arcR1, Math.PI * 1.8, Math.PI * 1.95, 7, 1.0, "81, 219, 255");
      bloomCtx.restore();

      bloomCtx.save();
      bloomCtx.rotate(r2);
      drawArc(bloomCtx, arcR2, 0, Math.PI * 0.7, 4.5, 0.7, "81, 219, 255");
      drawArc(bloomCtx, arcR2, Math.PI * 0.8, Math.PI * 1.8, 3.5, 0.5, "81, 219, 255");
      drawArc(bloomCtx, arcR2 - 4, Math.PI * 0.3, Math.PI * 0.6, 3, 0.4, "81, 219, 255");
      drawArc(bloomCtx, arcR2 - 4, Math.PI * 1.0, Math.PI * 1.5, 3, 0.4, "81, 219, 255");
      bloomCtx.restore();

      bloomCtx.save();
      bloomCtx.rotate(r1 * 1.3);
      for (let i = 0; i < blocks; i++) {
        const angle = (i * Math.PI * 2) / blocks;
        const noise = Math.sin(i * 0.4 + r1 * 6) * 4.5;
        const bh = 9 + logBass * 0.6 + noise;
        if ((i > 4 && i < 10) || (i > 32 && i < 38) || (i > 54 && i < 58)) continue;
        bloomCtx.save();
        bloomCtx.rotate(angle);
        bloomCtx.fillStyle = i % 6 === 0 ? "rgba(81, 219, 255, 0.95)" : "rgba(81, 219, 255, 0.7)";
        bloomCtx.fillRect(-3, -r3, 6, -bh);
        bloomCtx.restore();
      }
      bloomCtx.restore();

      bloomCtx.save();
      bloomCtx.rotate(-r1 * 0.7);
      bloomCtx.beginPath();
      bloomCtx.arc(0, 0, 165, 0, Math.PI * 2);
      bloomCtx.setLineDash([3, 6]);
      bloomCtx.strokeStyle = "rgba(81, 219, 255, 0.5)";
      bloomCtx.lineWidth = 3;
      bloomCtx.stroke();
      bloomCtx.setLineDash([]);
      drawArc(bloomCtx, 172, Math.PI * 0.0, Math.PI * 0.4, 5, 0.8, "81, 219, 255");
      drawArc(bloomCtx, 172, Math.PI * 0.9, Math.PI * 1.3, 5, 0.8, "81, 219, 255");
      drawArc(bloomCtx, 162, Math.PI * 1.5, Math.PI * 1.8, 3.5, 0.5, "81, 219, 255");
      bloomCtx.restore();

      bloomCtx.save();
      bloomCtx.rotate(r2 * 0.4);
      drawArc(bloomCtx, r5, 0, Math.PI * 0.6, 6, 0.9, "81, 219, 255");
      drawArc(bloomCtx, r5, Math.PI * 0.7, Math.PI * 1.3, 3.5, 0.5, "81, 219, 255");
      drawArc(bloomCtx, r5, Math.PI * 1.4, Math.PI * 1.9, 4.5, 0.7, "81, 219, 255");
      bloomCtx.restore();

      for (let i = pulses.length - 1; i >= 0; i--) {
        const p = pulses[i];
        bloomCtx.beginPath();
        bloomCtx.arc(0, 0, p.radius, 0, Math.PI * 2);
        bloomCtx.strokeStyle = `rgba(81, 219, 255, ${p.alpha * 1.5})`;
        bloomCtx.lineWidth = 7;
        bloomCtx.stroke();
      }

      if (audio && audio.timeDataArray) {
        const waveRadius = 278;
        const step = (Math.PI * 2) / audio.timeDataArray.length;
        bloomCtx.beginPath();
        for (let i = 0; i < audio.timeDataArray.length; i++) {
          const v = audio.timeDataArray[i] / 128.0 - 1.0;
          const a = i * step + r1 * 0.2;
          const wr = waveRadius + v * 18;
          const wx = Math.cos(a) * wr;
          const wy = Math.sin(a) * wr;
          if (i === 0) bloomCtx.moveTo(wx, wy);
          else bloomCtx.lineTo(wx, wy);
        }
        bloomCtx.closePath();
        bloomCtx.strokeStyle = "rgba(81, 219, 255, 0.7)";
        bloomCtx.lineWidth = 6;
        bloomCtx.stroke();

        bloomCtx.beginPath();
        for (let i = 0; i < audio.timeDataArray.length; i++) {
          const v = audio.timeDataArray[i] / 128.0 - 1.0;
          const a = i * step + r1 * 0.2 + 0.1;
          const wr = waveRadius + v * 13;
          const wx = Math.cos(a) * wr;
          const wy = Math.sin(a) * wr;
          if (i === 0) bloomCtx.moveTo(wx, wy);
          else bloomCtx.lineTo(wx, wy);
        }
        bloomCtx.strokeStyle = "rgba(81, 219, 255, 0.4)";
        bloomCtx.lineWidth = 3;
        bloomCtx.stroke();
      }

      bloomCtx.restore();
    }

    animRef.current = requestAnimationFrame(frame);
    return () => cancelAnimationFrame(animRef.current);
  }, []);

  return (
    <div
      // The core orb is a real UI element (click = audio unlock), so it must
      // never act as a window drag handle - only the empty space around it does.
      style={{
        position: "relative",
        width: "100%",
        height: "100%",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        overflow: "hidden",
      }}
      onClick={initAudio}
    >
      <canvas
        ref={bloomRef}
        data-no-drag
        style={{
          position: "absolute",
          width: "360px",
          height: "360px",
          pointerEvents: "none",
          filter: "blur(45px) brightness(2.2)",
          mixBlendMode: "screen",
          opacity: 0.8,
        }}
      />
      <canvas
        ref={canvasRef}
        data-no-drag
        style={{
          position: "absolute",
          width: "360px",
          height: "360px",
          filter: "drop-shadow(0 0 2px rgba(81, 219, 255, 0.9)) drop-shadow(0 0 15px rgba(0, 130, 255, 0.5)) contrast(1.25) saturate(1.4)",
        }}
      />
    </div>
  );
}
