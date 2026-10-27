import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';

// CVR 性能探针（短）：@1080x1920，负载足以让启动开销不再掩盖并行差异。
// 实际帧数不写死在这里，由 cvr_runtime.probe_frame_count() 按候选数决定：
// 探针太短时每个候选的耗时会被浏览器启动开销淹没，区分不出并行差异。
export const CvrProbeShort: React.FC = () => {
  const frame = useCurrentFrame();
  const items = Array.from({length: 30}, (_, i) => i);
  return (
    <AbsoluteFill style={{backgroundColor: '#101820'}}>
      {items.map((i) => {
        const angle = (frame * 2 + i * 7) % 360;
        const x = 50 + 350 * Math.cos((angle * Math.PI) / 180 + i);
        const y = 50 + 350 * Math.sin((angle * Math.PI) / 180 + i * 1.7);
        return (
          <div
            key={i}
            style={{
              position: 'absolute',
              left: x,
              top: y,
              width: 40,
              height: 40,
              borderRadius: 20,
              backgroundColor: `hsl(${(i * 13 + frame * 3) % 360}, 80%, 55%)`,
              transform: `scale(${0.6 + 0.4 * Math.sin(frame / 10 + i)})`,
            }}
          />
        );
      })}
    </AbsoluteFill>
  );
};
