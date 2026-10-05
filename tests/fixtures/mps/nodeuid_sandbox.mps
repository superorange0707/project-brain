<?xml version="1.0" encoding="UTF-8"?>
<model ref="r:b6a091bc-90a1-44ef-ac19-aa3bbb5aaa97(jetbrains.mps.samples.nodeuid.sandbox)">
  <persistence version="9" />
  <attribute name="doNotGenerate" value="true" />
  <languages>
    <use id="d1577ca1-c7c1-4121-9ada-937bdec4157c" name="jetbrains.mps.samples.nodeuid.lang" version="0" />
    <use id="f3061a53-9226-4cc5-a443-f952ceaf5816" name="jetbrains.mps.baseLanguage" version="12" />
  </languages>
  <imports />
  <registry>
    <language id="f3061a53-9226-4cc5-a443-f952ceaf5816" name="jetbrains.mps.baseLanguage">
      <concept id="1081236700938" name="jetbrains.mps.baseLanguage.structure.StaticMethodDeclaration" flags="ig" index="2YIFZL" />
      <concept id="1070534760951" name="jetbrains.mps.baseLanguage.structure.ArrayType" flags="in" index="10Q1$e">
        <child id="1070534760952" name="componentType" index="10Q1$1" />
      </concept>
      <concept id="1068390468198" name="jetbrains.mps.baseLanguage.structure.ClassConcept" flags="ig" index="312cEu" />
      <concept id="1068498886292" name="jetbrains.mps.baseLanguage.structure.ParameterDeclaration" flags="ir" index="37vLTG" />
      <concept id="1225271177708" name="jetbrains.mps.baseLanguage.structure.StringType" flags="in" index="17QB3L" />
      <concept id="4972933694980447171" name="jetbrains.mps.baseLanguage.structure.BaseVariableDeclaration" flags="ng" index="19Szcq">
        <child id="5680397130376446158" name="type" index="1tU5fm" />
      </concept>
      <concept id="1068580123132" name="jetbrains.mps.baseLanguage.structure.BaseMethodDeclaration" flags="ng" index="3clF44">
        <child id="1068580123133" name="returnType" index="3clF45" />
        <child id="1068580123134" name="parameter" index="3clF46" />
        <child id="1068580123135" name="body" index="3clF47" />
      </concept>
      <concept id="1068580123155" name="jetbrains.mps.baseLanguage.structure.ExpressionStatement" flags="nn" index="3clFbF">
        <child id="1068580123156" name="expression" index="3clFbG" />
      </concept>
      <concept id="1068580123136" name="jetbrains.mps.baseLanguage.structure.StatementList" flags="sn" stub="5293379017992965193" index="3clFbS">
        <child id="1068581517665" name="statement" index="3cqZAp" />
      </concept>
      <concept id="1068581517677" name="jetbrains.mps.baseLanguage.structure.VoidType" flags="in" index="3cqZAl" />
      <concept id="1107461130800" name="jetbrains.mps.baseLanguage.structure.Classifier" flags="ng" index="3pOWGL">
        <child id="5375687026011219971" name="member" index="jymVt" unordered="true" />
      </concept>
      <concept id="1178549954367" name="jetbrains.mps.baseLanguage.structure.IVisible" flags="ngI" index="1B3ioH">
        <child id="1178549979242" name="visibility" index="1B3o_S" />
      </concept>
      <concept id="1146644602865" name="jetbrains.mps.baseLanguage.structure.PublicVisibility" flags="nn" index="3Tm1VV" />
    </language>
    <language id="d1577ca1-c7c1-4121-9ada-937bdec4157c" name="jetbrains.mps.samples.nodeuid.lang">
      <concept id="5306028619768876669" name="jetbrains.mps.samples.nodeuid.lang.structure.ExpressionWithId" flags="ng" index="20KSan" />
    </language>
    <language id="ceab5195-25ea-4f22-9b92-103b95ca8c0c" name="jetbrains.mps.lang.core">
      <concept id="1169194658468" name="jetbrains.mps.lang.core.structure.INamedConcept" flags="ngI" index="TrEIO">
        <property id="1169194664001" name="name" index="TrG5h" />
      </concept>
    </language>
  </registry>
  <node concept="312cEu" id="4aN0LrcmCXa">
    <property role="TrG5h" value="Example" />
    <node concept="2YIFZL" id="4aN0LrcmCXR" role="jymVt">
      <property role="TrG5h" value="main" />
      <node concept="37vLTG" id="4aN0LrcmCXS" role="3clF46">
        <property role="TrG5h" value="args" />
        <node concept="10Q1$e" id="4aN0LrcmCXT" role="1tU5fm">
          <node concept="17QB3L" id="4aN0LrcmCXU" role="10Q1$1" />
        </node>
      </node>
      <node concept="3cqZAl" id="4aN0LrcmCXV" role="3clF45" />
      <node concept="3Tm1VV" id="4aN0LrcmCXW" role="1B3o_S" />
      <node concept="3clFbS" id="4aN0LrcmCXX" role="3clF47">
        <node concept="3clFbF" id="1d0mJ3Dzf4A" role="3cqZAp">
          <node concept="20KSan" id="1d0mJ3Dzf4$" role="3clFbG" />
        </node>
      </node>
    </node>
    <node concept="3Tm1VV" id="4aN0LrcmCXb" role="1B3o_S" />
  </node>
</model>

