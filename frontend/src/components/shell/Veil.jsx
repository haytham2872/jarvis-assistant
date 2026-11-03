import React from 'react'

export default function Veil({ show, onClick }) {
  return (
    <div
      className={'veil' + (show ? ' show' : '')}
      onClick={onClick}
    />
  )
}
