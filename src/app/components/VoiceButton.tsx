import { useState, useCallback } from "react";
import { Mic, MicOff } from "lucide-react";
import { motion } from "motion/react";
import { startListening, stopListening } from "../api";

const CYAN = "#00D9FF";
const MONO = "'Space Mono', monospace";

interface VoiceButtonProps {
  isListening: boolean;
  setIsListening: (v: boolean) => void;
  onTranscript: (text: string) => void;
}

export default function VoiceButton({
  isListening,
  setIsListening,
  onTranscript,
}: VoiceButtonProps) {
  const [loading, setLoading] = useState(false);

  const toggle = useCallback(async () => {
    if (loading) return;

    if (isListening) {
      setLoading(true);
      try {
        const result = await stopListening();
        setIsListening(false);
        if (result.text && result.text.trim()) {
          onTranscript(result.text.trim());
        }
      } catch (e) {
        console.error("Stop listening error:", e);
        setIsListening(false);
      } finally {
        setLoading(false);
      }
      } else {
        try {
          await startListening();
          setIsListening(true);
        } catch (e) {
          console.error("Start listening error:", e);
          setIsListening(false);
        }
      }
  }, [isListening, loading, setIsListening, onTranscript]);

  const rgb = "0, 217, 255";

  return (
    <button
      onClick={toggle}
      style={{ background: "none", border: "none", cursor: "pointer", padding: 0 }}
      className="flex flex-col items-center gap-2.5"
    >
      <motion.div
        whileTap={{ scale: 0.93 }}
        animate={
          isListening
            ? {
                boxShadow: [
                  `0 0 24px rgba(${rgb}, 0.35), inset 0 0 16px rgba(${rgb}, 0.08)`,
                  `0 0 40px rgba(${rgb}, 0.55), inset 0 0 24px rgba(${rgb}, 0.15)`,
                  `0 0 24px rgba(${rgb}, 0.35), inset 0 0 16px rgba(${rgb}, 0.08)`,
                ],
              }
            : {}
        }
        transition={
          isListening
            ? { duration: 1.5, repeat: Infinity, ease: "easeInOut" }
            : {}
        }
        style={{
          width: 54,
          height: 54,
          borderRadius: "50%",
          background: isListening
            ? `rgba(${rgb}, 0.14)`
            : "rgba(255, 255, 255, 0.04)",
          border: `1px solid ${isListening ? `rgba(${rgb}, 0.55)` : "rgba(255,255,255,0.09)"}`,
          backdropFilter: "blur(20px)",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          color: isListening ? CYAN : "rgba(255,255,255,0.42)",
          transition: "all 0.3s ease",
        }}
      >
        {loading ? (
          <div
            style={{
              width: 18,
              height: 18,
              border: `2px solid transparent`,
              borderTopColor: CYAN,
              borderRadius: "50%",
              animation: "spin 0.8s linear infinite",
            }}
          />
        ) : isListening ? (
          <MicOff size={18} />
        ) : (
          <Mic size={18} />
        )}
      </motion.div>
      <span
        style={{
          color: "rgba(255,255,255,0.22)",
          fontSize: "9px",
          letterSpacing: "0.22em",
          textTransform: "uppercase",
          fontFamily: MONO,
        }}
      >
        {isListening ? "Stop" : "Voice"}
      </span>
    </button>
  );
}
