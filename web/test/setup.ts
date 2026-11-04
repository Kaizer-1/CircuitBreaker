import '@testing-library/jest-dom/vitest'

// jsdom has no AnimationEvent; without it React listens for the old webkitAnimationEnd name and
// never sees `animationend` (components/Flash.tsx ends its flash on it).
if (!('AnimationEvent' in window)) {
  Object.defineProperty(window, 'AnimationEvent', {
    value: class AnimationEvent extends Event {
      readonly animationName: string
      constructor(type: string, init: AnimationEventInit = {}) {
        super(type, init)
        this.animationName = init.animationName ?? ''
      }
    },
  })
}
