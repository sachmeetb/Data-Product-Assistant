import React, { useState, useMemo } from 'react';

const TYPE_BADGE = {
  dimension: { label: 'DIMENSION', bg: '#eff6ff', color: '#1d4ed8', border: '#bfdbfe' },
  event:     { label: 'FACT / EVENT', bg: '#ecfdf5', color: '#047857', border: '#a7f3d0' },
  aggregate: { label: 'AGGREGATE', bg: '#fef3c7', color: '#b45309', border: '#fde68a' },
  bridge:    { label: 'BRIDGE', bg: '#f3e8ff', color: '#6d28d9', border: '#ddd6fe' },
};

export default function EntitySelectorCard({ view, onApplyScope, onOpenExistingStructure }) {
  const entities = useMemo(() => view?.entities || [], [view]);
  const domainName = view?.domain_name || 'Core Banking & Accounts';
  const standards = view?.standards || ['BIAN v12', 'ISO 20022'];

  // Selection modes: 'all' | 'single' | 'multiple'
  const [mode, setMode] = useState('multiple');
  const [selectedSet, setSelectedSet] = useState(() => {
    if (view?.selected_entities?.length) {
      return new Set(view.selected_entities);
    }
    return new Set(entities.map(e => e.table_name));
  });
  const [expandedTable, setExpandedTable] = useState(null);
  const [searchText, setSearchText] = useState('');

  const handleModeChange = (newMode) => {
    setMode(newMode);
    if (newMode === 'all') {
      setSelectedSet(new Set(entities.map(e => e.table_name)));
    } else if (newMode === 'single') {
      const first = Array.from(selectedSet)[0] || entities[0]?.table_name;
      setSelectedSet(first ? new Set([first]) : new Set());
    }
  };

  const handleEntityClick = (tableName) => {
    if (mode === 'single') {
      setSelectedSet(new Set([tableName]));
      return;
    }
    // If user clicks in 'all' mode, switch to 'multiple' automatically for smooth UX
    if (mode === 'all') {
      setMode('multiple');
    }
    setSelectedSet(prev => {
      const next = new Set(prev);
      if (next.has(tableName)) {
        next.delete(tableName);
      } else {
        next.add(tableName);
      }
      return next;
    });
  };

  const handleSelectOnlyThis = (e, tableName) => {
    e.stopPropagation();
    setMode('single');
    setSelectedSet(new Set([tableName]));
  };

  const filteredEntities = useMemo(() => {
    const q = searchText.trim().toLowerCase();
    if (!q) return entities;
    return entities.filter(ent =>
      ent.table_name.toLowerCase().includes(q) ||
      (ent.description || '').toLowerCase().includes(q) ||
      (ent.columns || []).some(c => (c.name || c.column_name || '').toLowerCase().includes(q))
    );
  }, [entities, searchText]);

  const selectedArray = useMemo(() => Array.from(selectedSet), [selectedSet]);

  const handleConfirm = () => {
    if (selectedArray.length === 0) return;
    if (onApplyScope) {
      onApplyScope({
        mode,
        domainName,
        industryId: view?.industry_id || 'banking',
        selectedEntities: selectedArray,
      });
    }
  };

  const handleOpenExisting = () => {
    const targetEntities = selectedArray.length > 0
      ? selectedArray
      : entities.map(e => e.table_name);
    if (onOpenExistingStructure) {
      onOpenExistingStructure({
        domainName,
        industryId: view?.industry_id || 'banking',
        selectedEntities: targetEntities,
      });
    }
  };

  return (
    <div style={{
      marginTop: 12,
      border: '1px solid #d8b4fe',
      borderRadius: 10,
      background: '#ffffff',
      boxShadow: '0 4px 16px rgba(70, 0, 115, 0.07)',
      overflow: 'hidden',
    }}>
      {/* Header */}
      <div style={{
        padding: '12px 16px',
        background: 'linear-gradient(135deg, #460073 0%, #6b21a8 100%)',
        color: '#ffffff',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 8,
      }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 15 }}>🧩</span>
            <span style={{ fontWeight: 700, fontSize: 13.5 }}>
              Silver Entity Scope &amp; Structure Selector — {domainName}
            </span>
          </div>
          <div style={{ fontSize: 11, opacity: 0.9, marginTop: 2 }}>
            Select all entities, a single specific entity, or a custom multi-entity subset — or open &amp; edit the existing Silver structure.
          </div>
        </div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {standards.map((std, i) => (
            <span key={i} style={{
              fontSize: 10,
              fontWeight: 700,
              padding: '2px 8px',
              borderRadius: 12,
              background: 'rgba(255,255,255,0.18)',
              color: '#fff',
            }}>
              {std}
            </span>
          ))}
        </div>
      </div>

      {/* Mode Switcher Bar */}
      <div style={{
        padding: '10px 16px',
        background: '#faf5ff',
        borderBottom: '1px solid #e9d5ff',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 10,
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexWrap: 'wrap' }}>
          <span style={{ fontSize: 11, fontWeight: 700, color: '#460073', marginRight: 4 }}>
            Selection Mode:
          </span>
          <button
            type="button"
            onClick={() => handleModeChange('all')}
            style={{
              padding: '5px 11px',
              borderRadius: 6,
              fontSize: 11.5,
              fontWeight: 700,
              cursor: 'pointer',
              border: mode === 'all' ? '1.5px solid #7500C0' : '1px solid #cbd5e1',
              background: mode === 'all' ? '#7500C0' : '#ffffff',
              color: mode === 'all' ? '#ffffff' : '#334155',
            }}
          >
            ✓ All Entities ({entities.length})
          </button>
          <button
            type="button"
            onClick={() => handleModeChange('single')}
            style={{
              padding: '5px 11px',
              borderRadius: 6,
              fontSize: 11.5,
              fontWeight: 700,
              cursor: 'pointer',
              border: mode === 'single' ? '1.5px solid #7500C0' : '1px solid #cbd5e1',
              background: mode === 'single' ? '#7500C0' : '#ffffff',
              color: mode === 'single' ? '#ffffff' : '#334155',
            }}
          >
            ◉ Single Entity (1)
          </button>
          <button
            type="button"
            onClick={() => handleModeChange('multiple')}
            style={{
              padding: '5px 11px',
              borderRadius: 6,
              fontSize: 11.5,
              fontWeight: 700,
              cursor: 'pointer',
              border: mode === 'multiple' ? '1.5px solid #7500C0' : '1px solid #cbd5e1',
              background: mode === 'multiple' ? '#7500C0' : '#ffffff',
              color: mode === 'multiple' ? '#ffffff' : '#334155',
            }}
          >
            ☑ Multiple Entities ({selectedArray.length})
          </button>
          {mode === 'multiple' && (
            <button
              type="button"
              onClick={() => setSelectedSet(new Set())}
              style={{
                padding: '4px 8px',
                borderRadius: 5,
                fontSize: 10.5,
                fontWeight: 600,
                cursor: 'pointer',
                border: '1px solid #e2e8f0',
                background: '#fff',
                color: '#64748b',
              }}
            >
              Clear
            </button>
          )}
        </div>

        <input
          type="text"
          value={searchText}
          onChange={e => setSearchText(e.target.value)}
          placeholder="Filter entities or columns..."
          style={{
            padding: '5px 10px',
            borderRadius: 6,
            border: '1px solid #cbd5e1',
            fontSize: 11.5,
            minWidth: 190,
            outline: 'none',
          }}
        />
      </div>

      {/* Entity Grid / List */}
      <div style={{
        padding: '12px 16px',
        maxHeight: 360,
        overflowY: 'auto',
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}>
        {filteredEntities.map(ent => {
          const isSelected = selectedSet.has(ent.table_name);
          const isExpanded = expandedTable === ent.table_name;
          const badge = TYPE_BADGE[(ent.type || ent.entity_type || 'dimension').toLowerCase()] || TYPE_BADGE.dimension;
          const cols = ent.columns || [];

          return (
            <div
              key={ent.table_name}
              style={{
                border: isSelected ? '1.5px solid #7500C0' : '1px solid #e2e8f0',
                borderRadius: 8,
                background: isSelected ? '#fcfaff' : '#ffffff',
                transition: 'all 0.15s ease',
              }}
            >
              <div
                onClick={() => handleEntityClick(ent.table_name)}
                style={{
                  padding: '9px 12px',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'space-between',
                  cursor: 'pointer',
                  gap: 10,
                  flexWrap: 'wrap',
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1, minWidth: 240 }}>
                  <input
                    type={mode === 'single' ? 'radio' : 'checkbox'}
                    checked={isSelected}
                    onChange={() => {}}
                    style={{ accentColor: '#7500C0', width: 15, height: 15, cursor: 'pointer' }}
                  />
                  <div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span style={{ fontFamily: 'monospace', fontWeight: 700, fontSize: 12.5, color: '#0f172a' }}>
                        {ent.table_name}
                      </span>
                      <span style={{
                        fontSize: 9.5,
                        fontWeight: 700,
                        padding: '1px 6px',
                        borderRadius: 4,
                        background: badge.bg,
                        color: badge.color,
                        border: `1px solid ${badge.border}`,
                      }}>
                        {badge.label}
                      </span>
                      <span style={{ fontSize: 10.5, color: '#64748b', fontWeight: 600 }}>
                        {cols.length} attributes
                      </span>
                    </div>
                    <div style={{ fontSize: 11, color: '#475569', marginTop: 2 }}>
                      {ent.description} · <span style={{ color: '#64748b', fontStyle: 'italic' }}>Grain: {ent.grain || 'one row per record'}</span>
                    </div>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <button
                    type="button"
                    onClick={(e) => handleSelectOnlyThis(e, ent.table_name)}
                    title="Select ONLY this entity"
                    style={{
                      padding: '3px 8px',
                      borderRadius: 5,
                      fontSize: 10.5,
                      fontWeight: 600,
                      border: '1px solid #d8b4fe',
                      background: isSelected && selectedArray.length === 1 ? '#7500C0' : '#f3e8ff',
                      color: isSelected && selectedArray.length === 1 ? '#ffffff' : '#6b21a8',
                      cursor: 'pointer',
                    }}
                  >
                    Only This
                  </button>
                  <button
                    type="button"
                    onClick={(e) => {
                      e.stopPropagation();
                      setExpandedTable(isExpanded ? null : ent.table_name);
                    }}
                    style={{
                      padding: '3px 8px',
                      borderRadius: 5,
                      fontSize: 10.5,
                      fontWeight: 600,
                      border: '1px solid #cbd5e1',
                      background: '#f8fafc',
                      color: '#334155',
                      cursor: 'pointer',
                    }}
                  >
                    {isExpanded ? 'Hide Columns ▲' : 'Inspect Columns ▼'}
                  </button>
                </div>
              </div>

              {isExpanded && (
                <div style={{
                  padding: '8px 12px 10px 36px',
                  borderTop: '1px solid #f1f5f9',
                  background: '#f8fafc',
                  display: 'grid',
                  gridTemplateColumns: 'repeat(auto-fill, minmax(210px, 1fr))',
                  gap: 6,
                }}>
                  {cols.map(c => {
                    const cname = c.name || c.column_name;
                    const dtype = c.data_type || c.type || 'STRING';
                    const isPk = c.is_pk || c.is_primary_key;
                    const fkRef = c.fk_ref || c.references;
                    const isPii = c.pii || c.is_pii;
                    return (
                      <div key={cname} style={{
                        padding: '4px 8px',
                        borderRadius: 5,
                        background: '#ffffff',
                        border: '1px solid #e2e8f0',
                        fontSize: 10.5,
                      }}>
                        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 4 }}>
                          <span style={{ fontFamily: 'monospace', fontWeight: 700, color: '#1e293b' }}>{cname}</span>
                          <span style={{ fontSize: 9.5, color: '#64748b', fontFamily: 'monospace' }}>{dtype}</span>
                        </div>
                        <div style={{ display: 'flex', gap: 4, marginTop: 2, flexWrap: 'wrap' }}>
                          {isPk && <span style={{ fontSize: 8.5, background: '#fef3c7', color: '#b45309', padding: '0 4px', borderRadius: 3, fontWeight: 700 }}>PK</span>}
                          {fkRef && <span style={{ fontSize: 8.5, background: '#e0f2fe', color: '#0369a1', padding: '0 4px', borderRadius: 3, fontWeight: 700 }}>FK → {fkRef}</span>}
                          {isPii && <span style={{ fontSize: 8.5, background: '#fee2e2', color: '#b91c1c', padding: '0 4px', borderRadius: 3, fontWeight: 700 }}>PII</span>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          );
        })}
      </div>

      {/* Footer Actions */}
      <div style={{
        padding: '10px 16px',
        background: '#f8fafc',
        borderTop: '1px solid #e2e8f0',
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: 10,
      }}>
        <div style={{ fontSize: 11.5, color: '#334155' }}>
          Selected Scope:{' '}
          <strong style={{ color: '#7500C0' }}>
            {selectedArray.length} of {entities.length} {selectedArray.length === 1 ? 'entity' : 'entities'}
          </strong>
          {selectedArray.length === 1 && (
            <span style={{ marginLeft: 6, fontFamily: 'monospace', fontSize: 11, background: '#f3e8ff', color: '#6b21a8', padding: '1px 6px', borderRadius: 4 }}>
              {selectedArray[0]}
            </span>
          )}
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
          <button
            type="button"
            onClick={handleOpenExisting}
            style={{
              padding: '7px 13px',
              borderRadius: 6,
              fontSize: 11.5,
              fontWeight: 700,
              cursor: 'pointer',
              border: '1px solid #7500C0',
              background: '#f3e8ff',
              color: '#460073',
            }}
          >
            🛠 Open &amp; Edit Existing Structure ({selectedArray.length || entities.length})
          </button>
          <button
            type="button"
            disabled={selectedArray.length === 0}
            onClick={handleConfirm}
            style={{
              padding: '7px 15px',
              borderRadius: 6,
              fontSize: 11.5,
              fontWeight: 700,
              cursor: selectedArray.length === 0 ? 'not-allowed' : 'pointer',
              border: 'none',
              background: selectedArray.length === 0 ? '#cbd5e1' : 'linear-gradient(135deg, #460073 0%, #7500C0 100%)',
              color: '#ffffff',
              boxShadow: selectedArray.length === 0 ? 'none' : '0 2px 6px rgba(117, 0, 192, 0.25)',
            }}
          >
            Continue with {selectedArray.length} {selectedArray.length === 1 ? 'Entity' : 'Entities'} →
          </button>
        </div>
      </div>
    </div>
  );
}
