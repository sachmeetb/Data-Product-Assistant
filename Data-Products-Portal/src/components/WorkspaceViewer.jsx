import React, { useState, useRef, useEffect } from 'react';
import { PRODUCTS } from '../App.jsx';
import { ArrowRight, RefreshCw, ExternalLink, Loader2, ArrowUpRight, Layers, Sparkles } from 'lucide-react';

export default function WorkspaceViewer({ currentProduct, onSwitchProduct, onBackToHub }) {
  const [isLoading, setIsLoading] = useState(true);
  const iframeRef = useRef(null);

  const reloadIframe = () => {
    setIsLoading(true);
    if (iframeRef.current) {
      iframeRef.current.src = currentProduct.url;
    }
  };

  useEffect(() => {
    setIsLoading(true);
  }, [currentProduct]);

  return (
    <div className="flex-1 flex flex-col overflow-hidden bg-gray-100">
      {/* Workspace Sub-Toolbar */}
      <div className="bg-white border-b px-4 py-2.5 flex items-center justify-between shadow-2xs border-[#E2DCE8]">
        <div className="flex items-center gap-3">
          <button
            onClick={onBackToHub}
            className="text-xs font-semibold text-gray-600 hover:text-purple-700 flex items-center gap-1.5 px-2.5 py-1.5 rounded hover:bg-gray-100 transition"
          >
            <ArrowRight size={13} className="rotate-180" /> Back to Products Hub
          </button>

          <div className="h-4 w-px bg-gray-200" />

          {/* Product Switcher Dropdown */}
          <div className="flex items-center gap-1.5">
            <span className="text-xs text-gray-400 font-medium hidden sm:inline">Active Product:</span>
            <div className="flex bg-gray-100 p-0.5 rounded-lg text-xs">
              {PRODUCTS.map(p => {
                const IconCmp = p.id === "silver" ? Layers : Sparkles;
                return (
                  <button
                    key={p.id}
                    onClick={() => onSwitchProduct(p)}
                    className={`px-2.5 py-1 rounded-md font-semibold transition flex items-center gap-1.5 ${
                      currentProduct.id === p.id 
                        ? "bg-white text-purple-900 shadow-2xs" 
                        : "text-gray-500 hover:text-gray-800"
                    }`}
                  >
                    <IconCmp size={11} />
                    <span>{p.title}</span>
                  </button>
                );
              })}
            </div>
          </div>
        </div>

        {/* Right Workspace Actions */}
        <div className="flex items-center gap-2">
          <button
            onClick={reloadIframe}
            title="Reload Product View"
            className="p-1.5 text-gray-500 hover:text-gray-800 hover:bg-gray-100 rounded transition text-xs flex items-center gap-1"
          >
            <RefreshCw size={13} className={isLoading ? "animate-spin" : ""} />
            <span className="hidden sm:inline">Reload</span>
          </button>

          <a
            href={currentProduct.url}
            target="_blank"
            rel="noopener noreferrer"
            title="Open in external browser window"
            className="px-2.5 py-1.5 text-xs font-semibold text-white rounded-md flex items-center gap-1.5 shadow-2xs transition bg-[#A100FF] hover:bg-[#7800C4]"
          >
            <span>Open in New Tab</span>
            <ExternalLink size={12} />
          </a>
        </div>
      </div>

      {/* Iframe Viewport Container */}
      <div className="flex-1 relative w-full bg-slate-50 overflow-hidden">
        {/* Loading Indicator Overlay */}
        {isLoading && (
          <div className="absolute inset-0 bg-white/75 backdrop-blur-xs flex flex-col items-center justify-center z-10">
            <Loader2 size={28} className="animate-spin text-purple-600 mb-2" />
            <div className="text-xs font-semibold text-gray-700">
              Connecting to {currentProduct.title}…
            </div>
            <div className="text-[11px] text-gray-400 mt-1 font-mono">
              {currentProduct.url}
            </div>
          </div>
        )}

        {/* Embedded Live Iframe */}
        <iframe
          ref={iframeRef}
          src={currentProduct.url}
          className="absolute inset-0 w-full h-full border-0"
          title={currentProduct.title}
          sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-downloads"
          onLoad={() => setIsLoading(false)}
        />

        {/* Friendly Frame Banner / Notice */}
        <div className="absolute bottom-3 right-4 z-20 pointer-events-auto">
          <div className="bg-white/95 backdrop-blur-md border border-[#E2DCE8] rounded-lg px-3 py-2 shadow-lg flex items-center gap-3 text-xs">
            <div className="flex items-center gap-2">
              <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse"></span>
              <span className="font-semibold text-gray-800">{currentProduct.title}</span>
              <span className="text-gray-400 font-mono text-[10px] hidden md:inline">({currentProduct.url})</span>
            </div>
            <a
              href={currentProduct.url}
              target="_blank"
              rel="noopener noreferrer"
              className="text-purple-700 hover:underline font-semibold flex items-center gap-1"
            >
              Full Screen Tab <ArrowUpRight size={12} />
            </a>
          </div>
        </div>
      </div>
    </div>
  );
}
