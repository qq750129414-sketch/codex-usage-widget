import AppKit
import SwiftUI

@main struct VisibilityTests {
 static func main(){
  if CommandLine.arguments.contains("--probe") {
   guard let app=NSRunningApplication.runningApplications(withBundleIdentifier:"com.openai.codex").first else {print("no-host");return}
   let began=ProcessInfo.processInfo.systemUptime
   print("page=\(ChatPageMonitor.inspect(pid:app.processIdentifier)) seconds=\(ProcessInfo.processInfo.systemUptime-began)")
   return
  }
  var checks=0
  func check(_ value:Bool){precondition(value);checks+=1}
  check(isChatComposer(role:"AXTextArea",label:"随心输入"))
  check(isChatComposer(role:"AXTextArea",label:"Ask anything"))
  check(!isChatComposer(role:"AXTextField",label:"随心输入"))
  check(!isChatComposer(role:"AXTextArea",label:"自定义指令"))
  check(!isChatComposer(role:"AXTextArea",label:""))
  for page in [ChatPageState.chat,.other,.unavailable] {
   check(!shouldShowUsagePanel(frontmost:"com.apple.finder",hiddenByUser:false,page:page))
   check(!shouldShowUsagePanel(frontmost:"com.openai.codex",hiddenByUser:true,page:page))
   check(shouldShowUsagePanel(frontmost:"com.openai.codex",hiddenByUser:false,page:page) == (page == .chat))
  }
  check(!shouldShowUsagePanel(frontmost:nil,hiddenByUser:false,page:.chat))
  let screen=NSRect(x:0,y:0,width:1800,height:1100)
  let full=NSRect(x:1400,y:20,width:300,height:446)
  let compact=fitPanelFrame(NSRect(x:full.maxX-300,y:full.minY,width:300,height:234),to:screen)
  check(compact.minY == full.minY && compact.maxX == full.maxX)
  check(compact.height == 240)
  check(panelSizeLimits(screen).min == NSSize(width:260,height:240))
  let host=NSRect(x:100,y:50,width:1500,height:1000)
  let restored=defaultPanelFrame(size:full.size,screen:screen,host:host)
  check(restored.size == full.size)
  check(restored.maxX == host.maxX-12 && restored.minY == host.minY+12)
  let leftScreen=NSRect(x:-1800,y:80,width:1800,height:1000)
  let fallback=defaultPanelFrame(size:compact.size,screen:leftScreen,host:nil)
  check(fallback.maxX == leftScreen.maxX-12 && fallback.minY == leftScreen.minY+12)
  check(fallback.size == compact.size && leftScreen.contains(fallback))
  let clipped=defaultPanelFrame(size:full.size,screen:screen,host:NSRect(x:100,y:-100,width:2000,height:1000))
  check(screen.contains(clipped) && clipped.size == full.size)
  for effort in ["ultra","Ultra","ULTRA","high","xhigh","max","medium","未知"] {
   let label=modelLineText(model:"gpt-6.1-sol",effort:effort)
   check(String(label.characters) == "模型：GPT-6.1 Sol "+effortDisplayName(effort))
   let tinted=label.runs.filter{$0.foregroundColor != nil}
   if effort.lowercased() == "ultra" {
    check(tinted.count == 1)
    check(tinted.first?.foregroundColor == ultraEffortColor)
    check(tinted.first.map{String(label[$0.range].characters)} == "Ultra")
   } else {check(tinted.isEmpty)}
  }
  let plain=modelLineText(model:"Custom Ultra Model",effort:"high")
  check(plain.runs.allSatisfy{$0.foregroundColor == nil})
  for (raw,label) in [("plus","Plus 1X"),("prolite","PRO 10X"),("pro","PRO 20X"),("promax","PRO 25X")] {
   let data=try! JSONSerialization.data(withJSONObject:["id":"codex","name":"Codex","planType":raw,"planLabel":label])
   let bucket=try! JSONDecoder().decode(Bucket.self,from:data)
   check(bucket.displayPlan == label)
   check(bucket.planType == raw)
  }
  let old=try! JSONDecoder().decode(Bucket.self,from:Data(#"{"id":"codex","name":"Codex","planType":"pro"}"#.utf8))
  check(old.displayPlan == "PRO")
  let unknown=try! JSONDecoder().decode(Bucket.self,from:Data(#"{"id":"codex","name":"Codex"}"#.utf8))
  check(unknown.displayPlan == "套餐未知")
  print("\(checks) visibility, geometry, plan and model-style checks passed")
 }
}
