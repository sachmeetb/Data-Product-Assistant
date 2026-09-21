import React from 'react';
import { PRODUCTS } from '../App.jsx';
import { Sparkles, Layers, ArrowUpRight, Maximize2, ExternalLink } from 'lucide-react';

export default function ProductHub({ onSelectProduct }) {
  return (
    <main className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16">
      {/* Header */}
      <div className="text-center max-w-2xl mx-auto mb-14">
        <div className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-semibold mb-3 border bg-[#F3E6FF] text-[#7800C4] border-[#E0C2FF]">
          <Sparkles size={13} className="text-[#A100FF]" /> Product Portfolio
        </div>
        <h1 className="text-3xl sm:text-4xl font-extrabold text-gray-900 tracking-tight">
          How can we help you
        </h1>
        <p className="mt-3 text-sm sm:text-base text-gray-600 leading-relaxed">
          Click on a product below to access its workspace.
        </p>
      </div>

      {/* 2 Clean Clickable Product Cards */}
      <div className="grid grid-cols-1 md:grid-cols-2 gap-8 max-w-4xl mx-auto">
        {PRODUCTS.map((prod) => {
          const isSilver = prod.id === "silver";
          const IconCmp = isSilver ? Layers : Sparkles;
          return (
            <div
              key={prod.id}
              className="card-hover bg-white rounded-2xl border border-[#E2DCE8] flex flex-col justify-between group cursor-pointer p-8 sm:p-10"
              onClick={() => onSelectProduct(prod, "workspace")}
            >
              <div>
                {/* Top Row: Icon & Direct Link Icon */}
                <div className="flex items-center justify-between mb-8">
                  <div 
                    className="w-16 h-16 rounded-2xl flex items-center justify-center text-white shadow-sm transition group-hover:scale-105 duration-200"
                    style={{ background: prod.gradient }}
                  >
                    <IconCmp size={32} />
                  </div>

                  <a
                    href={prod.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    onClick={e => e.stopPropagation()}
                    title="Open directly in new window"
                    className="p-3 text-gray-400 hover:text-purple-700 hover:bg-purple-50 rounded-xl transition"
                  >
                    <ArrowUpRight size={22} />
                  </a>
                </div>

                {/* Title Only */}
                <h2 className="text-2xl sm:text-3xl font-bold text-gray-900 group-hover:text-[#A100FF] transition leading-snug">
                  {prod.title}
                </h2>
              </div>

              {/* Card Actions Footer */}
              <div className="mt-12 pt-6 border-t border-gray-100 flex flex-col sm:flex-row items-center gap-3">
                <button
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    onSelectProduct(prod, "workspace");
                  }}
                  className="w-full sm:w-1/2 py-3 px-4 rounded-xl font-semibold text-xs text-gray-700 bg-white border border-gray-200 hover:bg-gray-100 flex items-center justify-center gap-2 transition"
                >
                  <Maximize2 size={14} /> Open In Portal
                </button>
                <a
                  href={prod.url}
                  target="_blank"
                  rel="noopener noreferrer"
                  onClick={e => e.stopPropagation()}
                  className="w-full sm:w-1/2 py-3 px-4 rounded-xl font-semibold text-xs text-white flex items-center justify-center gap-2 shadow-xs transition bg-[#A100FF] hover:bg-[#7800C4]"
                >
                  <ExternalLink size={14} /> Open in New Tab
                </a>
              </div>
            </div>
          );
        })}
      </div>
    </main>
  );
}
