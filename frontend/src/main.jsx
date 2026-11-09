import '@astryxdesign/core/reset.css';
import '@astryxdesign/core/astryx.css';
import '@astryxdesign/theme-neutral/theme.css';
import {createRoot} from 'react-dom/client';
import {Theme} from '@astryxdesign/core/theme';
import {InternationalizationProvider} from '@astryxdesign/core/i18n';
import ja from '@astryxdesign/core/locales/ja-JP.generated.js';
import {neutralTheme} from '@astryxdesign/theme-neutral/built';
import App from './App.jsx';

createRoot(document.getElementById('root')).render(<InternationalizationProvider locale="ja-JP" messages={{'ja-JP': ja}}><Theme theme={neutralTheme} mode="dark"><App /></Theme></InternationalizationProvider>);
