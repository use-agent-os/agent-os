import { useQuery } from '@tanstack/react-query'
import { authenticatedHeaders } from '@/lib/http-auth'
import { artifactDownloadUrl, type Artifact } from '@/views/chat/transcript/artifacts'

/** Fetch an artifact's JSON body (card payloads are not inline). */
export async function fetchArtifactJson(artifact: Artifact): Promise<unknown> {
  const url = artifactDownloadUrl(artifact)
  if (!url) throw new Error('missing download URL')
  // Already gateway-resolved (absolute off-gateway, root-relative on it).
  const response = await fetch(url, {
    headers: authenticatedHeaders(url),
    credentials: 'same-origin',
  })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  return response.json()
}

export function useArtifactJson(artifact: Artifact) {
  return useQuery({
    queryKey: ['artifact', artifact.id ?? artifactDownloadUrl(artifact)],
    queryFn: () => fetchArtifactJson(artifact),
    staleTime: Infinity,
  })
}
