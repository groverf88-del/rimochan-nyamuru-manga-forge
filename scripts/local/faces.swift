// 画像の顔・頭の位置を Vision で探して JSON で出す（compose.py の顔よけの補助）。
// 座標は左上原点の 0〜1。face = 顔の枠、head = 骨格の頭の点と首から出した頭の枠、human = 人の枠。
// 使い方: faces <画像>...   作り方: swiftc -O faces.swift -o faces（render.sh が自動で作る）
import Foundation
import Vision
import AppKit
var out: [String: Any] = [:]
func r3(_ v: [Double]) -> [Double] { v.map { ($0 * 1000).rounded() / 1000 } }
for path in CommandLine.arguments.dropFirst() {
    guard let img = NSImage(contentsOfFile: path),
          let cg = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else { continue }
    let W = Double(cg.width), H = Double(cg.height)
    let face = VNDetectFaceRectanglesRequest()
    let human = VNDetectHumanRectanglesRequest(); human.upperBodyOnly = false
    let pose = VNDetectHumanBodyPoseRequest()
    try? VNImageRequestHandler(cgImage: cg, options: [:]).perform([face, human, pose])
    func boxes(_ obs: [VNDetectedObjectObservation]?) -> [[Double]] {
        (obs ?? []).map { o in let b = o.boundingBox
            return r3([b.minX, 1 - b.maxY, b.maxX, 1 - b.minY, Double(o.confidence)]) }
    }
    var heads: [[Double]] = []
    for o in pose.results ?? [] {
        let pts = ((try? o.recognizedPoints(.face)) ?? [:]).values.filter { $0.confidence > 0.1 }
        guard !pts.isEmpty, let neck = try? o.recognizedPoint(.neck), neck.confidence > 0.3 else { continue }
        let hx = pts.map { Double($0.location.x) * W }.reduce(0, +) / Double(pts.count)
        let hy = pts.map { (1 - Double($0.location.y)) * H }.reduce(0, +) / Double(pts.count)
        let r = 1.3 * hypot(hx - Double(neck.location.x) * W, hy - (1 - Double(neck.location.y)) * H)
        heads.append(r3([(hx - r) / W, (hy - r * 1.1) / H, (hx + r) / W, (hy + r * 0.9) / H, Double(neck.confidence)]))
    }
    out[(path as NSString).lastPathComponent] = ["face": boxes(face.results), "human": boxes(human.results), "head": heads]
}
let d = try! JSONSerialization.data(withJSONObject: out, options: [.sortedKeys])
print(String(data: d, encoding: .utf8)!)
