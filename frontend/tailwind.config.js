/** @type {import('tailwindcss').Config} */
export default {
  content: ['./index.html', './src/**/*.{js,ts,jsx,tsx}'],
  theme: {
    extend: {
      colors: {
        ink: '#101828',
        paper: '#f7f8fa',
        accent: '#2457e6',
      },
      boxShadow: {
        card: '0 1px 2px rgba(16,24,40,.06), 0 8px 30px rgba(16,24,40,.05)',
      },
    },
  },
  plugins: [],
}
