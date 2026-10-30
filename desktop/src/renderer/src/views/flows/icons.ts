import { createElement } from 'react'
import {
  Box,
  Combine,
  Download,
  Eye,
  ImagePlus,
  PenLine,
  Sparkles,
  Type,
  type LucideIcon,
  type LucideProps,
} from 'lucide-react'
import type { NodeGroup } from './types'

/** Icons for the v1 nodes; anything else falls back by group. */
const BY_ID: Record<string, LucideIcon> = {
  text: Type,
  load_image: ImagePlus,
  join_text: Combine,
  prompt_writer: PenLine,
  describe_image: Eye,
  image: Sparkles,
  save: Download,
}

const BY_GROUP: Record<NodeGroup, LucideIcon> = {
  input: Type,
  utility: Combine,
  ai: Sparkles,
  output: Download,
}

export function nodeIcon(id: string, group?: NodeGroup): LucideIcon {
  return BY_ID[id] ?? (group ? BY_GROUP[group] : Box) ?? Box
}

/** The node's icon as an element (a fixed component, whatever the node). */
export function NodeIcon({ id, group, ...props }: { id: string; group?: NodeGroup } & LucideProps) {
  return createElement(nodeIcon(id, group), props)
}
