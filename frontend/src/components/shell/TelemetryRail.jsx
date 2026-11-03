import React from 'react'
import AmplitudeMeter   from '../rail/AmplitudeMeter'
import PipelineIndicator from '../rail/PipelineIndicator'
import MoodGauge        from '../rail/MoodGauge'

export default function TelemetryRail() {
  return (
    <div className="rail">
      <AmplitudeMeter />
      <MoodGauge />
      <PipelineIndicator />
    </div>
  )
}
