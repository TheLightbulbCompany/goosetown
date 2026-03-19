import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter } from 'react-router-dom';
import App from './App.tsx';
import './index.css';
import 'uplot/dist/uPlot.min.css';
import 'react-toastify/dist/ReactToastify.css';
import TownProvider from './components/TownProvider.tsx';

console.log("[GooseTown] Frontend loaded — monorepo deploy test");
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrowserRouter>
      <TownProvider>
        <App />
      </TownProvider>
    </BrowserRouter>
  </React.StrictMode>,
);
