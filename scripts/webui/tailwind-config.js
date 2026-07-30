      tailwind = {
        config: {
          theme: {
            extend: {
              colors: {
                ink: {
                  bg:'rgb(var(--ink-bg) / <alpha-value>)', card:'rgb(var(--ink-card) / <alpha-value>)',
                  elevated:'rgb(var(--ink-elevated) / <alpha-value>)', input:'rgb(var(--ink-input) / <alpha-value>)',
                  border:'rgb(var(--ink-border) / <alpha-value>)', deep:'rgb(var(--ink-deep) / <alpha-value>)'
                },
                gold: {
                  DEFAULT:'rgb(var(--gold) / <alpha-value>)', light:'rgb(var(--gold-light) / <alpha-value>)',
                  muted:'rgb(var(--gold-muted) / <alpha-value>)', dim:'rgb(var(--gold-dim) / <alpha-value>)'
                },
                cream: {
                  DEFAULT:'rgb(var(--cream) / <alpha-value>)', muted:'rgb(var(--cream-muted) / <alpha-value>)',
                  subtle:'rgb(var(--cream-subtle) / <alpha-value>)'
                },
                success:'rgb(var(--success) / <alpha-value>)',
                danger:'rgb(var(--danger) / <alpha-value>)',
              },
              fontFamily: {
                serif: ['"Noto Serif SC"', 'STSong', 'Georgia', 'serif'],
                mono:  ['"JetBrains Mono"', '"Cascadia Code"', 'Consolas', 'monospace'],
                sans:  ['"IBM Plex Sans"', '"Microsoft YaHei UI"', 'sans-serif'],
              },
              animation: { 'fade-in': 'fadeIn 0.4s ease-out both' },
              keyframes: { fadeIn: { '0%': { opacity:'0' }, '100%': { opacity:'1' } } }
            }
          }
        }
      };
