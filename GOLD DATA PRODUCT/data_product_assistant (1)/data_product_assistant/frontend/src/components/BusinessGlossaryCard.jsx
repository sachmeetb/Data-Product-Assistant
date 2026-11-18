import { useState, useEffect, useMemo } from 'react'
import { saveSessionGlossary } from '../api/chat'

const SQL_TYPES = ['STRING', 'NUMERIC', 'INT64', 'FLOAT64', 'BOOL', 'DATE', 'TIMESTAMP', 'JSON']
const EDITABLE_ROLES = ['ATTRIBUTE', 'KPI', 'PII']

function normalizeEntitiesFromGlossary(glossary) {
  if (!glossary) return []
  if (Array.isArray(glossary.entities) && glossary.entities.length > 0) {
    return glossary.entities.map(ent => ({
      table_name: ent.table_name || 'gold_entity',
      entity_type: (ent.entity_type || 'DIMENSION').toUpperCase(),
      grain: ent.grain || '1 row per entity record',
      description: ent.description || '',
      attributes: (ent.attributes || []).map(attr => {
        const isPk = Boolean(attr.is_pk || attr.role === 'PK' || attr.type_label === 'PK')
        const fkRef = attr.fk_ref || null
        const lockedKey = Boolean(isPk || fkRef || attr.locked_key)
        const role = isPk
          ? 'PK'
          : fkRef
            ? 'FK'
            : (attr.role || attr.type_label || (attr.pii && attr.pii !== 'NONE' ? 'PII' : 'ATTRIBUTE')).toUpperCase()
        return {
          ...attr,
          column_name: attr.column_name || attr.name || 'attr_col',
          name: attr.column_name || attr.name || 'attr_col',
          sql_type: (attr.sql_type || attr.data_type || 'STRING').toUpperCase(),
          data_type: (attr.sql_type || attr.data_type || 'STRING').toUpperCase(),
          role,
          type_label: role,
          is_pk: isPk,
          fk_ref: fkRef,
          pii: role === 'PII' ? (attr.pii && attr.pii !== 'NONE' ? attr.pii : 'CONFIDENTIAL') : (attr.pii || 'NONE'),
          nullable: isPk ? false : (attr.nullable !== undefined ? Boolean(attr.nullable) : true),
          locked_key: lockedKey,
          included: lockedKey ? true : (attr.included !== undefined ? Boolean(attr.included) : true),
          description: attr.description || '',
        }
      }),
    }))
  }

  // Fallback if only flat entries are present
  const grouped = {}
  for (const entry of (glossary.entries || [])) {
    const entName = entry.entity_name || 'gold_conformed_entity'
    if (!grouped[entName]) {
      grouped[entName] = {
        table_name: entName,
        entity_type: (entry.entity_type || 'DIMENSION').toUpperCase(),
        grain: '1 row per conformed record',
        description: '',
        attributes: [],
      }
    }
    const isPk = Boolean(entry.is_pk || entry.type_label === 'PK')
    const fkRef = entry.fk_ref || null
    const lockedKey = Boolean(isPk || fkRef || entry.locked_key)
    const role = isPk ? 'PK' : fkRef ? 'FK' : (entry.type_label || 'ATTRIBUTE').toUpperCase()
    grouped[entName].attributes.push({
      ...entry,
      column_name: entry.column_name || entry.name,
      name: entry.column_name || entry.name,
      sql_type: (entry.sql_type || 'STRING').toUpperCase(),
      data_type: (entry.sql_type || 'STRING').toUpperCase(),
      role,
      type_label: role,
      is_pk: isPk,
      fk_ref: fkRef,
      pii: role === 'PII' ? 'CONFIDENTIAL' : (entry.pii || 'NONE'),
      nullable: isPk ? false : (entry.nullable !== undefined ? Boolean(entry.nullable) : true),
      locked_key: lockedKey,
      included: lockedKey ? true : (entry.included !== undefined ? Boolean(entry.included) : true),
      description: entry.description || '',
    })
  }
  return Object.values(grouped)
}

function roleBadgeStyle(role) {
  switch ((role || '').toUpperCase()) {
    case 'PK':
      return { background: '#FEF3C7', color: '#92400E', border: '1px solid #F59E0B' }
    case 'FK':
      return { background: '#F3E8FF', color: '#6B21A8', border: '1px solid #C084FC' }
    case 'PII':
      return { background: '#FEE2E2', color: '#991B1B', border: '1px solid #FCA5A5' }
    case 'KPI':
      return { background: '#DCFCE7', color: '#166534', border: '1px solid #86EFAC' }
    default:
      return { background: '#DBEAFE', color: '#1E40AF', border: '1px solid #93C5FD' }
  }
}

const ROW_GRID_TEMPLATE = '34px minmax(175px, 1.15fr) 108px 108px 66px minmax(250px, 2.2fr)'

export default function BusinessGlossaryCard({
  glossary,
  sessionId,
  onGlossaryChange,
  onSaveGlossary,
  onConfirmWithGlossary,
}) {
  const [entities, setEntities] = useState(() => normalizeEntitiesFromGlossary(glossary))
  const [activeEntityTab, setActiveEntityTab] = useState('ALL')
  const [collapsedMap, setCollapsedMap] = useState({})
  const [searchQuery, setSearchQuery] = useState('')
  const [roleFilter, setRoleFilter] = useState('ALL')
  const [saving, setSaving] = useState(false)
  const [savedNotice, setSavedNotice] = useState('')
  const [dirty, setDirty] = useState(false)

  // Re-sync when server sends a new glossary payload
  useEffect(() => {
    const normalized = normalizeEntitiesFromGlossary(glossary)
    setEntities(normalized)
    setDirty(false)
    if (activeEntityTab !== 'ALL' && !normalized.some(e => e.table_name === activeEntityTab)) {
      setActiveEntityTab('ALL')
    }
  }, [glossary])

  const currentGlossaryPayload = useMemo(() => {
    const flatEntries = []
    for (const ent of entities) {
      for (const attr of (ent.attributes || [])) {
        if (attr.included !== false) {
          flatEntries.push({
            ...attr,
            entity_name: ent.table_name,
            entity_type: ent.entity_type,
            name: attr.column_name,
            qualified_name: `${ent.table_name}.${attr.column_name}`,
          })
        }
      }
    }
    return {
      ...(glossary || {}),
      entity_count: entities.length,
      column_count: flatEntries.length,
      selected_entities: entities.map(e => e.table_name),
      entities,
      entries: flatEntries,
    }
  }, [entities, glossary])

  // Notify parent App.jsx whenever glossary state changes so chip clicks ("Confirm Requirement") inherit edits
  useEffect(() => {
    if (onGlossaryChange) {
      onGlossaryChange(currentGlossaryPayload)
    }
  }, [currentGlossaryPayload, onGlossaryChange])

  const totalAttributes = useMemo(
    () => entities.reduce((acc, e) => acc + (e.attributes?.length || 0), 0),
    [entities]
  )
  const activeAttributes = useMemo(
    () => entities.reduce((acc, e) => acc + (e.attributes?.filter(a => a.included !== false).length || 0), 0),
    [entities]
  )

  const toggleEntityCollapse = (tableName) => {
    setCollapsedMap(prev => ({ ...prev, [tableName]: !prev[tableName] }))
  }

  const handleAttrChange = (tableName, attrIdx, field, value) => {
    setDirty(true)
    setSavedNotice('')
    setEntities(prev => prev.map(ent => {
      if (ent.table_name !== tableName) return ent
      const nextAttrs = ent.attributes.map((attr, idx) => {
        if (idx !== attrIdx) return attr
        // Guardrail 2: protect PK/FK from exclusion or renaming
        if (attr.locked_key && (field === 'included' || field === 'column_name' || field === 'role')) {
          return attr
        }
        const updated = { ...attr, [field]: value }
        if (field === 'column_name') {
          updated.name = value
        }
        if (field === 'sql_type') {
          updated.data_type = value
        }
        if (field === 'role') {
          updated.type_label = value
          updated.pii = value === 'PII' ? 'CONFIDENTIAL' : 'NONE'
        }
        return updated
      })
      return { ...ent, attributes: nextAttrs }
    }))
  }

  const handleAddAttribute = (tableName) => {
    setDirty(true)
    setSavedNotice('')
    setCollapsedMap(prev => ({ ...prev, [tableName]: false }))
    setEntities(prev => prev.map(ent => {
      if (ent.table_name !== tableName) return ent
      const customIdx = (ent.attributes?.length || 0) + 1
      const newColName = `custom_attr_${customIdx}`
      const newAttr = {
        id: `${tableName}.${newColName}_${Date.now()}`,
        entity_name: tableName,
        entity_type: ent.entity_type,
        column_name: newColName,
        name: newColName,
        sql_type: 'STRING',
        data_type: 'STRING',
        role: 'ATTRIBUTE',
        type_label: 'ATTRIBUTE',
        is_pk: false,
        fk_ref: null,
        pii: 'NONE',
        nullable: true,
        locked_key: false,
        included: true,
        is_custom: true,
        description: `Custom business attribute added to ${tableName}.`,
      }
      return {
        ...ent,
        attributes: [...(ent.attributes || []), newAttr],
      }
    }))
  }

  const handleRemoveCustomAttribute = (tableName, attrIdx) => {
    setDirty(true)
    setSavedNotice('')
    setEntities(prev => prev.map(ent => {
      if (ent.table_name !== tableName) return ent
      return {
        ...ent,
        attributes: ent.attributes.filter((attr, idx) => idx !== attrIdx || attr.locked_key),
      }
    }))
  }

  const handleSaveChanges = async () => {
    setSaving(true)
    setSavedNotice('')
    try {
      if (sessionId) {
        await saveSessionGlossary(sessionId, currentGlossaryPayload)
      }
      if (onSaveGlossary) {
        onSaveGlossary(currentGlossaryPayload)
      }
      setDirty(false)
      setSavedNotice('Saved to Gold session & contract')
    } catch (err) {
      setSavedNotice(`Saved locally (${err.message || 'sync'})`)
      if (onSaveGlossary) {
        onSaveGlossary(currentGlossaryPayload)
      }
    } finally {
      setSaving(false)
    }
  }

  const handleConfirmAndProceed = () => {
    if (onConfirmWithGlossary) {
      onConfirmWithGlossary(currentGlossaryPayload)
    }
  }

  const visibleEntities = useMemo(() => {
    return entities
      .filter(ent => activeEntityTab === 'ALL' || ent.table_name === activeEntityTab)
      .map(ent => {
        const filteredAttrs = (ent.attributes || []).filter(attr => {
          if (roleFilter === 'PK_FK' && !attr.locked_key) return false
          if (roleFilter === 'KPI' && attr.role !== 'KPI') return false
          if (roleFilter === 'PII' && attr.role !== 'PII' && (!attr.pii || attr.pii === 'NONE')) return false
          if (roleFilter === 'ATTRIBUTE' && attr.role !== 'ATTRIBUTE') return false
          if (searchQuery.trim()) {
            const q = searchQuery.toLowerCase()
            const matchName = (attr.column_name || '').toLowerCase().includes(q)
            const matchDesc = (attr.description || '').toLowerCase().includes(q)
            const matchRole = (attr.role || '').toLowerCase().includes(q)
            return matchName || matchDesc || matchRole
          }
          return true
        })
        return { ...ent, filteredAttrs }
      })
  }, [entities, activeEntityTab, roleFilter, searchQuery])

  return (
    <div
      className="bg-card"
      style={{
        display: 'block',
        width: '100%',
        marginTop: 10,
        padding: '16px 18px',
        border: '1px solid #CBD5E1',
        borderRadius: 12,
        background: '#F8FAFC',
        boxSizing: 'border-box',
      }}
    >
      {/* Top Header */}
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'flex-start',
          gap: 12,
          flexWrap: 'wrap',
          marginBottom: 12,
        }}
      >
        <div style={{ flex: '1 1 380px', minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <span style={{ fontSize: 15, fontWeight: 800, color: '#460073' }}>
              Business Glossary &amp; Attribute Workbench
            </span>
            <span
              style={{
                background: '#F3E8FF',
                color: '#6B21A8',
                border: '1px solid #D8B4FE',
                fontWeight: 700,
                padding: '2px 10px',
                borderRadius: 999,
                fontSize: 11,
                whiteSpace: 'nowrap',
              }}
            >
              {entities.length} {entities.length === 1 ? 'Entity' : 'Entities'}
            </span>
            <span
              style={{
                background: '#DBEAFE',
                color: '#1E40AF',
                border: '1px solid #93C5FD',
                fontWeight: 700,
                padding: '2px 10px',
                borderRadius: 999,
                fontSize: 11,
                whiteSpace: 'nowrap',
              }}
            >
              {activeAttributes} / {totalAttributes} Active Attributes
            </span>
            {dirty && (
              <span
                style={{
                  background: '#FEF3C7',
                  color: '#92400E',
                  border: '1px solid #F59E0B',
                  fontSize: 11,
                  fontWeight: 700,
                  padding: '2px 8px',
                  borderRadius: 999,
                }}
              >
                Unsaved inline edits
              </span>
            )}
            {savedNotice && (
              <span
                style={{
                  background: '#DCFCE7',
                  color: '#166534',
                  border: '1px solid #86EFAC',
                  fontSize: 11,
                  fontWeight: 700,
                  padding: '2px 8px',
                  borderRadius: 999,
                }}
              >
                ✓ {savedNotice}
              </span>
            )}
          </div>
          <p style={{ margin: '5px 0 0', fontSize: 12.5, color: '#475569', lineHeight: 1.45 }}>
            Dynamically scoped to your selected Gold entities ({glossary?.domain_name || 'Active Domain'}).
            Edit column names, SQL data types, KPI/PII roles, nullability, or business definitions below.
          </p>
        </div>

        <div style={{ display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap', flexShrink: 0 }}>
          <button
            type="button"
            onClick={handleSaveChanges}
            disabled={saving}
            style={{
              padding: '7px 13px',
              fontSize: 12,
              fontWeight: 700,
              borderRadius: 8,
              border: '1px solid #C084FC',
              background: '#FFFFFF',
              color: '#6B21A8',
              cursor: saving ? 'wait' : 'pointer',
              whiteSpace: 'nowrap',
            }}
          >
            {saving ? 'Saving…' : 'Save Glossary Changes'}
          </button>
          {onConfirmWithGlossary && (
            <button
              type="button"
              onClick={handleConfirmAndProceed}
              style={{
                padding: '7px 14px',
                fontSize: 12,
                fontWeight: 700,
                borderRadius: 8,
                border: 'none',
                background: '#7500C0',
                color: '#FFFFFF',
                cursor: 'pointer',
                whiteSpace: 'nowrap',
                boxShadow: '0 1px 3px rgba(117, 0, 192, 0.25)',
              }}
            >
              Apply Glossary &amp; Proceed to Contract →
            </button>
          )}
        </div>
      </div>

      {/* Entity Filter Tabs */}
      <div
        style={{
          display: 'flex',
          gap: 6,
          flexWrap: 'wrap',
          marginBottom: 10,
          paddingBottom: 10,
          borderBottom: '1px solid #E2E8F0',
        }}
      >
        <button
          type="button"
          onClick={() => setActiveEntityTab('ALL')}
          style={{
            padding: '5px 12px',
            fontSize: 12,
            fontWeight: 700,
            borderRadius: 6,
            border: activeEntityTab === 'ALL' ? '1px solid #7500C0' : '1px solid #CBD5E1',
            background: activeEntityTab === 'ALL' ? '#7500C0' : '#FFFFFF',
            color: activeEntityTab === 'ALL' ? '#FFFFFF' : '#334155',
            cursor: 'pointer',
          }}
        >
          All Selected Entities ({entities.length})
        </button>
        {entities.map(ent => {
          const isActive = activeEntityTab === ent.table_name
          const activeCount = (ent.attributes || []).filter(a => a.included !== false).length
          return (
            <button
              key={ent.table_name}
              type="button"
              onClick={() => setActiveEntityTab(ent.table_name)}
              style={{
                padding: '5px 11px',
                fontSize: 12,
                fontWeight: 600,
                borderRadius: 6,
                border: isActive ? '1px solid #7500C0' : '1px solid #CBD5E1',
                background: isActive ? '#F3E8FF' : '#FFFFFF',
                color: isActive ? '#581C87' : '#334155',
                cursor: 'pointer',
                display: 'inline-flex',
                alignItems: 'center',
                gap: 6,
              }}
            >
              <span style={{ fontFamily: 'Consolas, Monaco, monospace' }}>{ent.table_name}</span>
              <span
                style={{
                  fontSize: 10.5,
                  fontWeight: 700,
                  background: isActive ? '#D8B4FE' : '#E2E8F0',
                  color: isActive ? '#460073' : '#475569',
                  padding: '1px 6px',
                  borderRadius: 99,
                }}
              >
                {activeCount}
              </span>
            </button>
          )
        })}
      </div>

      {/* Search & Role Filter Bar */}
      <div
        style={{
          display: 'flex',
          gap: 8,
          alignItems: 'center',
          marginBottom: 12,
          flexWrap: 'wrap',
        }}
      >
        <input
          type="text"
          value={searchQuery}
          onChange={e => setSearchQuery(e.target.value)}
          placeholder="Search attributes by column name, role, or business definition…"
          style={{
            flex: '1 1 260px',
            padding: '7px 11px',
            fontSize: 12.5,
            borderRadius: 7,
            border: '1px solid #CBD5E1',
            background: '#FFFFFF',
            color: '#0F172A',
          }}
        />
        <div style={{ display: 'flex', gap: 5, alignItems: 'center', flexWrap: 'wrap' }}>
          {['ALL', 'PK_FK', 'KPI', 'PII', 'ATTRIBUTE'].map(r => (
            <button
              key={r}
              type="button"
              onClick={() => setRoleFilter(r)}
              style={{
                padding: '5px 9px',
                fontSize: 11,
                fontWeight: 700,
                borderRadius: 6,
                border: roleFilter === r ? '1px solid #460073' : '1px solid #CBD5E1',
                background: roleFilter === r ? '#460073' : '#FFFFFF',
                color: roleFilter === r ? '#FFFFFF' : '#475569',
                cursor: 'pointer',
              }}
            >
              {r === 'PK_FK' ? '🔒 Keys (PK/FK)' : r}
            </button>
          ))}
        </div>
      </div>

      {/* Scrollable Entity Sections — Block layout (never flex-shrinks children) */}
      <div
        style={{
          display: 'block',
          maxHeight: 540,
          overflowY: 'auto',
          overflowX: 'auto',
          paddingRight: 2,
        }}
      >
        {visibleEntities.map(ent => {
          const allAttrs = ent.attributes || []
          const activeCount = allAttrs.filter(a => a.included !== false).length
          const isCollapsed = Boolean(collapsedMap[ent.table_name]) && activeEntityTab === 'ALL'

          return (
            <div
              key={ent.table_name}
              style={{
                display: 'block',
                marginBottom: 14,
                border: '1px solid #CBD5E1',
                borderRadius: 10,
                background: '#FFFFFF',
                boxShadow: '0 1px 2px rgba(15, 23, 42, 0.04)',
              }}
            >
              {/* Entity Section Header */}
              <div
                style={{
                  background: '#F1F5F9',
                  padding: '10px 14px',
                  borderBottom: isCollapsed ? 'none' : '1px solid #CBD5E1',
                  borderRadius: isCollapsed ? 10 : '10px 10px 0 0',
                  display: 'flex',
                  justifyContent: 'space-between',
                  alignItems: 'center',
                  flexWrap: 'wrap',
                  gap: 10,
                }}
              >
                <div
                  onClick={() => activeEntityTab === 'ALL' && toggleEntityCollapse(ent.table_name)}
                  style={{
                    display: 'flex',
                    alignItems: 'center',
                    gap: 8,
                    flexWrap: 'wrap',
                    cursor: activeEntityTab === 'ALL' ? 'pointer' : 'default',
                  }}
                >
                  {activeEntityTab === 'ALL' && (
                    <span style={{ fontSize: 11, color: '#475569', width: 14, display: 'inline-block' }}>
                      {isCollapsed ? '▶' : '▼'}
                    </span>
                  )}
                  <span style={{ fontFamily: 'Consolas, Monaco, monospace', fontWeight: 800, fontSize: 13.5, color: '#0F172A' }}>
                    {ent.table_name}
                  </span>
                  <span
                    style={{
                      fontSize: 10,
                      fontWeight: 800,
                      padding: '2px 7px',
                      borderRadius: 4,
                      background: ent.entity_type === 'EVENT' ? '#DBEAFE' : ent.entity_type === 'AGGREGATE' ? '#FEF3C7' : '#F3E8FF',
                      color: ent.entity_type === 'EVENT' ? '#1E40AF' : ent.entity_type === 'AGGREGATE' ? '#92400E' : '#6B21A8',
                    }}
                  >
                    {ent.entity_type}
                  </span>
                  <span style={{ fontSize: 12, color: '#475569' }}>
                    Grain: <strong>{ent.grain}</strong>
                  </span>
                  <span
                    style={{
                      fontSize: 11,
                      fontWeight: 700,
                      color: '#334155',
                      background: '#E2E8F0',
                      padding: '2px 8px',
                      borderRadius: 99,
                    }}
                  >
                    {activeCount}/{allAttrs.length} active
                  </span>
                </div>

                <button
                  type="button"
                  onClick={() => handleAddAttribute(ent.table_name)}
                  style={{
                    padding: '5px 11px',
                    fontSize: 11.5,
                    fontWeight: 700,
                    borderRadius: 6,
                    border: '1px solid #C084FC',
                    background: '#FFFFFF',
                    color: '#7500C0',
                    cursor: 'pointer',
                  }}
                >
                  + Add Attribute
                </button>
              </div>

              {/* Column Header Bar + Attribute Grid Rows */}
              {!isCollapsed && (
                <div style={{ display: 'block', minWidth: 760 }}>
                  {/* Grid Column Headers */}
                  <div
                    style={{
                      display: 'grid',
                      gridTemplateColumns: ROW_GRID_TEMPLATE,
                      gap: 10,
                      alignItems: 'center',
                      padding: '7px 14px',
                      background: '#F8FAFC',
                      borderBottom: '1px solid #E2E8F0',
                      fontSize: 10.5,
                      fontWeight: 800,
                      color: '#64748B',
                      textTransform: 'uppercase',
                      letterSpacing: '0.04em',
                    }}
                  >
                    <div style={{ textAlign: 'center' }}>Use</div>
                    <div>Column Name</div>
                    <div>Role / Key</div>
                    <div>SQL Type</div>
                    <div style={{ textAlign: 'center' }}>Null</div>
                    <div>Business Glossary Definition</div>
                  </div>

                  {/* Attribute Rows */}
                  {ent.filteredAttrs.map((attr) => {
                    const realIdx = allAttrs.findIndex(a => a === attr || a.column_name === attr.column_name)
                    const isIncluded = attr.included !== false
                    const badgeStyle = roleBadgeStyle(attr.role)

                    return (
                      <div
                        key={attr.id || `${ent.table_name}-${realIdx}`}
                        style={{
                          display: 'grid',
                          gridTemplateColumns: ROW_GRID_TEMPLATE,
                          gap: 10,
                          alignItems: 'center',
                          padding: '9px 14px',
                          borderBottom: '1px solid #F1F5F9',
                          background: !isIncluded ? '#F8FAFC' : '#FFFFFF',
                          opacity: !isIncluded ? 0.55 : 1,
                          boxSizing: 'border-box',
                        }}
                      >
                        {/* 1. Include / Exclude Checkbox */}
                        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
                          <input
                            type="checkbox"
                            checked={isIncluded}
                            disabled={attr.locked_key}
                            onChange={e => handleAttrChange(ent.table_name, realIdx, 'included', e.target.checked)}
                            title={
                              attr.locked_key
                                ? 'Primary & Foreign Keys are locked to preserve relational integrity across Gold entities'
                                : 'Include or exclude this attribute from the Gold Data Contract & DDL'
                            }
                            style={{
                              width: 15,
                              height: 15,
                              cursor: attr.locked_key ? 'not-allowed' : 'pointer',
                            }}
                          />
                        </div>

                        {/* 2. Column Name */}
                        <div style={{ minWidth: 0 }}>
                          {attr.locked_key ? (
                            <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                              <span
                                style={{
                                  fontFamily: 'Consolas, Monaco, monospace',
                                  fontWeight: 700,
                                  fontSize: 12.5,
                                  color: '#0F172A',
                                  background: '#F1F5F9',
                                  padding: '4px 8px',
                                  borderRadius: 5,
                                  border: '1px solid #E2E8F0',
                                  display: 'inline-block',
                                  overflow: 'hidden',
                                  textOverflow: 'ellipsis',
                                  whiteSpace: 'nowrap',
                                }}
                                title={attr.fk_ref ? `Foreign Key → ${attr.fk_ref}` : 'Primary Key (locked)'}
                              >
                                🔒 {attr.column_name}
                              </span>
                              {attr.fk_ref && (
                                <span
                                  style={{
                                    fontSize: 10.5,
                                    color: '#6B21A8',
                                    fontFamily: 'Consolas, Monaco, monospace',
                                    whiteSpace: 'nowrap',
                                    overflow: 'hidden',
                                    textOverflow: 'ellipsis',
                                  }}
                                  title={`References ${attr.fk_ref}`}
                                >
                                  ↳ {attr.fk_ref}
                                </span>
                              )}
                            </div>
                          ) : (
                            <input
                              type="text"
                              value={attr.column_name}
                              onChange={e => handleAttrChange(ent.table_name, realIdx, 'column_name', e.target.value)}
                              style={{
                                width: '100%',
                                fontFamily: 'Consolas, Monaco, monospace',
                                fontWeight: 600,
                                fontSize: 12.5,
                                color: '#0F172A',
                                padding: '5px 8px',
                                borderRadius: 5,
                                border: '1px solid #CBD5E1',
                                background: '#FFFFFF',
                                boxSizing: 'border-box',
                              }}
                              title="Edit technical column name"
                            />
                          )}
                        </div>

                        {/* 3. Role Badge / Selector */}
                        <div>
                          {attr.locked_key ? (
                            <span
                              style={{
                                ...badgeStyle,
                                display: 'inline-block',
                                fontSize: 11,
                                fontWeight: 800,
                                padding: '3px 9px',
                                borderRadius: 5,
                                textAlign: 'center',
                              }}
                            >
                              {attr.role}
                            </span>
                          ) : (
                            <select
                              value={EDITABLE_ROLES.includes(attr.role) ? attr.role : 'ATTRIBUTE'}
                              onChange={e => handleAttrChange(ent.table_name, realIdx, 'role', e.target.value)}
                              style={{
                                ...badgeStyle,
                                width: '100%',
                                fontSize: 11,
                                fontWeight: 700,
                                padding: '4px 6px',
                                borderRadius: 5,
                                cursor: 'pointer',
                                boxSizing: 'border-box',
                              }}
                              title="Change business classification (Attribute, KPI Measure, or PII)"
                            >
                              {EDITABLE_ROLES.map(r => (
                                <option key={r} value={r}>{r}</option>
                              ))}
                            </select>
                          )}
                        </div>

                        {/* 4. SQL Data Type Selector */}
                        <div>
                          <select
                            value={SQL_TYPES.includes(attr.sql_type) ? attr.sql_type : 'STRING'}
                            onChange={e => handleAttrChange(ent.table_name, realIdx, 'sql_type', e.target.value)}
                            style={{
                              width: '100%',
                              fontFamily: 'Consolas, Monaco, monospace',
                              fontSize: 11.5,
                              fontWeight: 600,
                              color: '#334155',
                              background: '#F8FAFC',
                              border: '1px solid #CBD5E1',
                              borderRadius: 5,
                              padding: '4px 6px',
                              cursor: 'pointer',
                              boxSizing: 'border-box',
                            }}
                            title="Select Databricks SQL / ANSI SQL data type"
                          >
                            {SQL_TYPES.map(t => (
                              <option key={t} value={t}>{t}</option>
                            ))}
                          </select>
                        </div>

                        {/* 5. Nullable Checkbox */}
                        <div style={{ display: 'flex', justifyContent: 'center', alignItems: 'center' }}>
                          <input
                            type="checkbox"
                            checked={attr.is_pk ? false : Boolean(attr.nullable)}
                            disabled={attr.is_pk}
                            onChange={e => handleAttrChange(ent.table_name, realIdx, 'nullable', e.target.checked)}
                            title={attr.is_pk ? 'Primary keys cannot be nullable' : 'Toggle column nullability'}
                            style={{
                              width: 15,
                              height: 15,
                              cursor: attr.is_pk ? 'not-allowed' : 'pointer',
                            }}
                          />
                        </div>

                        {/* 6. Editable Business Glossary Definition + Remove Custom Button */}
                        <div style={{ display: 'flex', alignItems: 'center', gap: 6, minWidth: 0 }}>
                          <input
                            type="text"
                            value={attr.description || ''}
                            onChange={e => handleAttrChange(ent.table_name, realIdx, 'description', e.target.value)}
                            placeholder="Enter plain-English business glossary definition…"
                            style={{
                              flex: 1,
                              width: '100%',
                              fontSize: 12.5,
                              color: '#1E293B',
                              padding: '5px 9px',
                              borderRadius: 5,
                              border: '1px solid #CBD5E1',
                              background: '#FFFFFF',
                              boxSizing: 'border-box',
                            }}
                          />
                          {attr.is_custom && (
                            <button
                              type="button"
                              onClick={() => handleRemoveCustomAttribute(ent.table_name, realIdx)}
                              style={{
                                padding: '4px 8px',
                                background: '#FEE2E2',
                                border: '1px solid #FCA5A5',
                                borderRadius: 5,
                                color: '#991B1B',
                                fontSize: 11,
                                fontWeight: 700,
                                cursor: 'pointer',
                                flexShrink: 0,
                              }}
                              title="Remove custom attribute"
                            >
                              ✕
                            </button>
                          )}
                        </div>
                      </div>
                    )
                  })}
                </div>
              )}
            </div>
          )
        })}
      </div>

      {/* Footer Note */}
      <div
        style={{
          marginTop: 10,
          paddingTop: 10,
          borderTop: '1px solid #E2E8F0',
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          flexWrap: 'wrap',
          gap: 8,
        }}
      >
        <p style={{ margin: 0, fontSize: 11.5, color: '#64748B', lineHeight: 1.45 }}>
          🔒 <strong>Relational Key Guardrail:</strong> Primary (<code>PK</code>) and Foreign (<code>FK</code>) keys are protected from deletion to keep join paths intact, while their business definitions remain editable. All changes propagate to your <strong>ODCS Data Contract</strong>, <strong>Databricks SQL DDL</strong>, and <strong>STTM Workbook</strong>.
        </p>
      </div>
    </div>
  )
}

