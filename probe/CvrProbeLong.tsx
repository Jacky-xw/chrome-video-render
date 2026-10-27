import React from 'react';
import {AbsoluteFill, useCurrentFrame} from 'remotion';

// CVR 性能探针（长）：@1080x1920，元素数量与计算量为短探针的数倍，
// 用于测长视频的分段并行。实际帧数同样由 probe_frame_count() 按候选数决定。
export const CvrProbeLong: React.FC = () => {
  const frame = useCurrentFrame();
  const items = Array.from({length: 150}, (_, i) => i);
  return (
    <AbsoluteFill style={{backgroundColor: '#0b1620'}}>
      {items.map((i) => {
        // 每帧做一段小循环，制造足以反映 CPU 差异的计算量。
        let acc = 0;
        for (let k = 0; k < 60; k++) {
          acc += Math.sin((frame + i + k) * 0.01) * Math.cos(k * 0.03);
        }
        const angle = (frame * 1.5 + i * 3) % 360;
        const x = 50 + 450 * Math.cos((angle * Math.PI) / 180 + i * 0.6);
        const y = 50 + 450 * Math.sin((angle * Math.PI) / 180 + i * 1.1);
        return (
          <div
            key={i}
            style={{
              position: 'absolute',
              left: x,
              top: y,
              width: 20,
              height: 20,
              borderRadius: acc > 0 ? 10 : 4,
              backgroundColor: `hsl(${(i * 7 + frame * 2) % 360}, 75%, 50%)`,
              opacity: 0.75,
            }}
          />
        );
      })}
    </AbsoluteFill>
  );
};
