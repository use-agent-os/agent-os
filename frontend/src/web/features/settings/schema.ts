// config.schema -> form fields (overhaul-control-ui D9, task 7.9). The schema
// is pydantic's JSON Schema for GatewayConfig: `$ref` into `$defs`, optional
// values as `anyOf: [{…}, {type: 'null'}]`, enums, descriptions, defaults.
import {
  fieldKind,
  flattenEntries,
  isReadonlyKey,
  isSensitiveKey,
  type ConfigData,
} from '@/views/config/logic'
import type { FieldType } from './fields'

export interface JsonSchema {
  $ref?: string
  type?: string | string[]
  enum?: unknown[]
  const?: unknown
  anyOf?: JsonSchema[]
  oneOf?: JsonSchema[]
  allOf?: JsonSchema[]
  properties?: Record<string, JsonSchema>
  additionalProperties?: boolean | JsonSchema
  items?: JsonSchema
  description?: string
  title?: string
  default?: unknown
  format?: string
  readOnly?: boolean
  writeOnly?: boolean
  $defs?: Record<string, JsonSchema>
  [key: string]: unknown
}

export interface SchemaField {
  path: string
  /** The dotted path: dozens of leaves share titles such as "Api Key". */
  label: string
  /** The schema's title, when it says more than the path's last segment. */
  title?: string
  help?: string
  type: FieldType
  optional: boolean
  /** Whether the schema describes this path (false: typed from its value). */
  described: boolean
}

/** Follow `$ref`s (and single-entry `allOf` wrappers) to the node itself. */
export function resolveRef(node: JsonSchema | undefined, root: JsonSchema): JsonSchema | undefined {
  let current = node
  for (let guard = 0; current && guard < 16; guard++) {
    if (current.$ref) {
      const name = current.$ref.replace(/^#\/(\$defs|definitions)\//, '')
      const target =
        root.$defs?.[name] ?? (root.definitions as Record<string, JsonSchema> | undefined)?.[name]
      current = target ? { ...target, ...withoutRef(current) } : undefined
      continue
    }
    if (current.allOf?.length === 1 && !current.properties) {
      current = { ...current.allOf[0]!, ...withoutKey(current, 'allOf') }
      continue
    }
    return current
  }
  return current
}

function withoutRef(node: JsonSchema): JsonSchema {
  const { $ref: _ref, ...rest } = node
  void _ref
  return rest
}

function withoutKey(node: JsonSchema, key: string): JsonSchema {
  const copy = { ...node }
  delete copy[key]
  return copy
}

/** `anyOf` with a `null` branch is an optional value of the other branch. */
export function unwrapOptional(
  node: JsonSchema,
  root: JsonSchema,
): { node: JsonSchema; optional: boolean } {
  const variants = node.anyOf ?? node.oneOf
  if (!variants) return { node, optional: false }
  const nonNull = variants.filter((v) => v.type !== 'null')
  if (nonNull.length === 1 && nonNull.length < variants.length) {
    const inner = resolveRef(nonNull[0], root) ?? nonNull[0]!
    return {
      node: {
        ...inner,
        description: node.description ?? inner.description,
        default: node.default ?? inner.default,
        title: node.title ?? inner.title,
      },
      optional: true,
    }
  }
  return { node, optional: false }
}

/** The schema node for a dotted path, or undefined when the schema does not describe it. */
export function schemaAt(
  root: JsonSchema,
  path: string,
): { node: JsonSchema; optional: boolean } | undefined {
  let node: JsonSchema | undefined = root
  let optional = false
  for (const part of path.split('.')) {
    node = resolveRef(node, root)
    if (!node) return undefined
    const unwrapped = unwrapOptional(node, root)
    node = unwrapped.node
    const child: JsonSchema | undefined =
      node.properties?.[part] ??
      (typeof node.additionalProperties === 'object' ? node.additionalProperties : undefined)
    if (!child) return undefined
    node = child
  }
  node = resolveRef(node, root)
  if (!node) return undefined
  const unwrapped = unwrapOptional(node, root)
  optional = unwrapped.optional
  return { node: resolveRef(unwrapped.node, root) ?? unwrapped.node, optional }
}

function typeOf(node: JsonSchema): string {
  return Array.isArray(node.type) ? (node.type.find((t) => t !== 'null') ?? '') : (node.type ?? '')
}

function titleFor(path: string, node?: JsonSchema): string | undefined {
  const title = node?.title?.trim()
  if (!title) return undefined
  // pydantic derives "Api Key" from `api_key`: that repeats the path.
  const last = path.split('.').pop() ?? ''
  return title.toLowerCase().replace(/[\s-]+/g, '_') === last.toLowerCase() ? undefined : title
}

/** Build the Advanced form's fields: every config leaf, typed from the schema when it can be. */
export function fieldsFromSchema(config: ConfigData, schema: JsonSchema | null): SchemaField[] {
  return flattenEntries(Object.entries(config)).map(([path, value]) => {
    const found = schema ? schemaAt(schema, path) : undefined
    const sensitive = isSensitiveKey(path)
    if (!found) {
      const kind = fieldKind(path, value)
      const type: FieldType =
        kind === 'object'
          ? { kind: 'json' }
          : kind === 'string'
            ? { kind: 'string', secret: sensitive }
            : { kind }
      return { path, label: path, type, optional: false, described: false }
    }
    const { node, optional } = found
    const help = node.description
    let type: FieldType
    if (isReadonlyKey(path) || node.readOnly) type = { kind: 'readonly' }
    else if (Array.isArray(node.enum))
      type = { kind: 'enum', options: node.enum.map((v) => String(v)) }
    else {
      const t = typeOf(node)
      if (t === 'boolean') type = { kind: 'boolean' }
      else if (t === 'integer' || t === 'number') type = { kind: 'number' }
      else if (t === 'object' || t === 'array') type = { kind: 'json' }
      else
        type = {
          kind: 'string',
          secret: sensitive || node.format === 'password' || node.writeOnly === true,
        }
    }
    return { path, label: path, title: titleFor(path, node), help, type, optional, described: true }
  })
}
