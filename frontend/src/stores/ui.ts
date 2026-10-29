import { create } from 'zustand'

type UIState = {
  navOpen: boolean
  setNavOpen: (value: boolean) => void
}

export const useUI = create<UIState>((set) => ({
  navOpen: false,
  setNavOpen: (navOpen) => set({ navOpen }),
}))
