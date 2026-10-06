import { useCallback, useState } from 'react'
import {
  Background,
  Controls,
  Handle,
  MarkerType,
  MiniMap,
  Position,
  ReactFlow,
  addEdge,
  type Connection,
  type Edge,
  type Node,
  type NodeProps,
} from '@xyflow/react'

type PortType = 'labelled_collection' | 'fitted_quantifier' | 'evaluation'

type LabNodeData = {
  title: string
  subtitle: string
  input?: PortType
  output?: PortType
}

const initialNodes: Node<LabNodeData>[] = [
  {
    id: 'dataset',
    type: 'labNode',
    position: { x: 40, y: 130 },
    data: {
      title: 'Dataset',
      subtitle: 'Labelled CSV',
      output: 'labelled_collection',
    },
  },
  {
    id: 'acc',
    type: 'labNode',
    position: { x: 340, y: 130 },
    data: {
      title: 'ACC',
      subtitle: 'Adjusted Classify & Count',
      input: 'labelled_collection',
      output: 'fitted_quantifier',
    },
  },
  {
    id: 'evaluation',
    type: 'labNode',
    position: { x: 640, y: 130 },
    data: {
      title: 'Evaluation',
      subtitle: 'APP · MAE',
      input: 'fitted_quantifier',
      output: 'evaluation',
    },
  },
]

const initialEdges: Edge[] = [
  {
    id: 'dataset-acc',
    source: 'dataset',
    target: 'acc',
    markerEnd: { type: MarkerType.ArrowClosed },
  },
  {
    id: 'acc-evaluation',
    source: 'acc',
    target: 'evaluation',
    markerEnd: { type: MarkerType.ArrowClosed },
  },
]

function LabNode({ data }: NodeProps<Node<LabNodeData>>) {
  return (
    <article className="lab-node">
      {data.input && (
        <Handle
          id="input"
          type="target"
          position={Position.Left}
          className={`port port-${data.input}`}
        />
      )}
      <div className="lab-node__kind">{data.input ?? 'source'}</div>
      <strong>{data.title}</strong>
      <span>{data.subtitle}</span>
      {data.output && (
        <Handle
          id="output"
          type="source"
          position={Position.Right}
          className={`port port-${data.output}`}
        />
      )}
    </article>
  )
}

const nodeTypes = { labNode: LabNode }

export function App() {
  const [nodes] = useState(initialNodes)
  const [edges, setEdges] = useState(initialEdges)

  const isValidConnection = useCallback(
    (connection: Edge | Connection) => {
      const source = nodes.find((node) => node.id === connection.source)
      const target = nodes.find((node) => node.id === connection.target)
      return Boolean(source?.data.output && source.data.output === target?.data.input)
    },
    [nodes],
  )

  const onConnect = useCallback(
    (connection: Connection) => {
      if (isValidConnection(connection)) {
        setEdges((current) => addEdge(connection, current))
      }
    },
    [isValidConnection],
  )

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Architecture spike</p>
          <h1>QuaPy Lab</h1>
        </div>
        <span className="status">Typed workflow canvas</span>
      </header>
      <section className="canvas" aria-label="Quantification workflow editor">
        <ReactFlow
          nodes={nodes}
          edges={edges}
          nodeTypes={nodeTypes}
          onConnect={onConnect}
          isValidConnection={isValidConnection}
          fitView
        >
          <Background gap={24} size={1} />
          <MiniMap pannable zoomable />
          <Controls />
        </ReactFlow>
      </section>
    </main>
  )
}

