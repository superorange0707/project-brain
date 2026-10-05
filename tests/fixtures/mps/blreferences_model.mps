<?xml version="1.0" encoding="UTF-8"?>
<model ref="r:fa2d3540-4c85-418c-9027-d922b5dcb05b(jetbrains.mps.samples.blReferences.model)">
  <persistence version="9" />
  <languages>
    <use id="4b7c56eb-9347-4ed1-a5d9-938a850b9e1d" name="jetbrains.mps.baseLanguage.date" version="0" />
    <use id="1c5b433b-3a0b-47c4-bed8-d496b01eb018" name="jetbrains.mps.baseLanguage.box" version="0" />
    <use id="f3061a53-9226-4cc5-a443-f952ceaf5816" name="jetbrains.mps.baseLanguage" version="12" />
  </languages>
  <imports>
    <import index="wyt6" ref="6354ebe7-c22a-4a0f-ac54-50b52ab9b065/java:java.lang(JDK/)" implicit="true" />
    <import index="guwi" ref="6354ebe7-c22a-4a0f-ac54-50b52ab9b065/java:java.io(JDK/)" implicit="true" />
  </imports>
  <registry>
    <language id="f3061a53-9226-4cc5-a443-f952ceaf5816" name="jetbrains.mps.baseLanguage">
      <concept id="1215693861676" name="jetbrains.mps.baseLanguage.structure.BaseAssignmentExpression" flags="nn" index="d038R">
        <child id="1068498886297" name="rValue" index="37vLTx" />
        <child id="1068498886295" name="lValue" index="37vLTJ" />
      </concept>
      <concept id="1215695189714" name="jetbrains.mps.baseLanguage.structure.PlusAssignmentExpression" flags="nn" index="d57v9" />
      <concept id="1202948039474" name="jetbrains.mps.baseLanguage.structure.InstanceMethodCallOperation" flags="nn" index="liA8E" />
      <concept id="1465982738277781862" name="jetbrains.mps.baseLanguage.structure.PlaceholderMember" flags="nn" index="2tJIrI" />
      <concept id="1197027756228" name="jetbrains.mps.baseLanguage.structure.DotExpression" flags="nn" index="2OqwBi">
        <child id="1197027771414" name="operand" index="2Oq$k0" />
        <child id="1197027833540" name="operation" index="2OqNvi" />
      </concept>
      <concept id="1145552977093" name="jetbrains.mps.baseLanguage.structure.GenericNewExpression" flags="nn" index="2ShNRf">
        <child id="1145553007750" name="creator" index="2ShVmc" />
      </concept>
      <concept id="1070475926800" name="jetbrains.mps.baseLanguage.structure.StringLiteral" flags="nn" index="Xl_RD">
        <property id="1070475926801" name="value" index="Xl_RC" />
      </concept>
      <concept id="1081236700938" name="jetbrains.mps.baseLanguage.structure.StaticMethodDeclaration" flags="ig" index="2YIFZL" />
      <concept id="1070533707846" name="jetbrains.mps.baseLanguage.structure.StaticFieldReference" flags="nn" index="10M0yZ">
        <reference id="1144433057691" name="classifier" index="1PxDUh" />
      </concept>
      <concept id="1070534760951" name="jetbrains.mps.baseLanguage.structure.ArrayType" flags="in" index="10Q1$e">
        <child id="1070534760952" name="componentType" index="10Q1$1" />
      </concept>
      <concept id="1068390468198" name="jetbrains.mps.baseLanguage.structure.ClassConcept" flags="ig" index="312cEu" />
      <concept id="1068431474542" name="jetbrains.mps.baseLanguage.structure.VariableDeclaration" flags="ng" index="33uBYm">
        <child id="1068431790190" name="initializer" index="33vP2m" />
      </concept>
      <concept id="1068498886296" name="jetbrains.mps.baseLanguage.structure.VariableReference" flags="nn" index="37vLTw">
        <reference id="1068581517664" name="variableDeclaration" index="3cqZAo" />
      </concept>
      <concept id="1068498886292" name="jetbrains.mps.baseLanguage.structure.ParameterDeclaration" flags="ir" index="37vLTG" />
      <concept id="1068498886294" name="jetbrains.mps.baseLanguage.structure.AssignmentExpression" flags="nn" index="37vLTI" />
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
      <concept id="1068580123157" name="jetbrains.mps.baseLanguage.structure.Statement" flags="nn" index="3clFbH" />
      <concept id="1068580123136" name="jetbrains.mps.baseLanguage.structure.StatementList" flags="sn" stub="5293379017992965193" index="3clFbS">
        <child id="1068581517665" name="statement" index="3cqZAp" />
      </concept>
      <concept id="1068580320020" name="jetbrains.mps.baseLanguage.structure.IntegerConstant" flags="nn" index="3cmrfG">
        <property id="1068580320021" name="value" index="3cmrfH" />
      </concept>
      <concept id="1068581242875" name="jetbrains.mps.baseLanguage.structure.PlusExpression" flags="nn" index="3cpWs3" />
      <concept id="1068581242864" name="jetbrains.mps.baseLanguage.structure.LocalVariableDeclarationStatement" flags="nn" index="3cpWs8">
        <child id="1068581242865" name="localVariableDeclaration" index="3cpWs9" />
      </concept>
      <concept id="1068581242863" name="jetbrains.mps.baseLanguage.structure.LocalVariableDeclaration" flags="nr" index="3cpWsn" />
      <concept id="1068581517677" name="jetbrains.mps.baseLanguage.structure.VoidType" flags="in" index="3cqZAl" />
      <concept id="1204053956946" name="jetbrains.mps.baseLanguage.structure.IMethodCall" flags="ngI" index="1ndlxa">
        <reference id="1068499141037" name="baseMethodDeclaration" index="37wK5l" />
        <child id="1068499141038" name="actualArgument" index="37wK5m" />
      </concept>
      <concept id="1107461130800" name="jetbrains.mps.baseLanguage.structure.Classifier" flags="ng" index="3pOWGL">
        <child id="5375687026011219971" name="member" index="jymVt" unordered="true" />
      </concept>
      <concept id="7812454656619025412" name="jetbrains.mps.baseLanguage.structure.LocalMethodCall" flags="nn" index="1rXfSq" />
      <concept id="1081773326031" name="jetbrains.mps.baseLanguage.structure.BinaryOperation" flags="nn" index="3uHJSO">
        <child id="1081773367579" name="rightExpression" index="3uHU7w" />
        <child id="1081773367580" name="leftExpression" index="3uHU7B" />
      </concept>
      <concept id="1178549954367" name="jetbrains.mps.baseLanguage.structure.IVisible" flags="ngI" index="1B3ioH">
        <child id="1178549979242" name="visibility" index="1B3o_S" />
      </concept>
      <concept id="1146644602865" name="jetbrains.mps.baseLanguage.structure.PublicVisibility" flags="nn" index="3Tm1VV" />
    </language>
    <language id="1c5b433b-3a0b-47c4-bed8-d496b01eb018" name="jetbrains.mps.baseLanguage.box">
      <concept id="8995237445478421944" name="jetbrains.mps.baseLanguage.box.structure.BoxType" flags="ig" index="WRhlO">
        <child id="8995237445478421945" name="type" index="WRhlP" />
      </concept>
      <concept id="8995237445478422284" name="jetbrains.mps.baseLanguage.box.structure.BoxCreator" flags="ng" index="WRhv0">
        <child id="8995237445478422361" name="value" index="WRhul" />
        <child id="5371100132593285855" name="type" index="1aNO72" />
      </concept>
      <concept id="8995237445478594044" name="jetbrains.mps.baseLanguage.box.structure.Box_ValueOperation" flags="ng" index="WRFkK" />
    </language>
    <language id="ceab5195-25ea-4f22-9b92-103b95ca8c0c" name="jetbrains.mps.lang.core">
      <concept id="1169194658468" name="jetbrains.mps.lang.core.structure.INamedConcept" flags="ngI" index="TrEIO">
        <property id="1169194664001" name="name" index="TrG5h" />
      </concept>
    </language>
    <language id="4b7c56eb-9347-4ed1-a5d9-938a850b9e1d" name="jetbrains.mps.baseLanguage.date">
      <concept id="3825148134790537826" name="jetbrains.mps.baseLanguage.date.structure.DateLiteral" flags="ng" index="1Ypomk">
        <property id="3825148134790537827" name="value" index="1Ypoml" />
      </concept>
      <concept id="3825148134790521554" name="jetbrains.mps.baseLanguage.date.structure.DateType" flags="ig" index="1Yu$k$" />
    </language>
  </registry>
  <node concept="312cEu" id="58TlvqhXvNt">
    <property role="TrG5h" value="BlReferencesSandbox" />
    <node concept="2tJIrI" id="58TlvqhXvO9" role="jymVt" />
    <node concept="2YIFZL" id="58TlvqhXRbi" role="jymVt">
      <property role="TrG5h" value="foo" />
      <node concept="3clFbS" id="58TlvqhXRbm" role="3clF47">
        <node concept="3cpWs8" id="58TlvqhXRbn" role="3cqZAp">
          <node concept="3cpWsn" id="58TlvqhXRbo" role="3cpWs9">
            <property role="TrG5h" value="date" />
            <node concept="1Yu$k$" id="58TlvqhXRbp" role="1tU5fm" />
            <node concept="1Ypomk" id="58TlvqhXRbq" role="33vP2m">
              <property role="1Ypoml" value="2001-01-01" />
            </node>
          </node>
        </node>
        <node concept="3clFbF" id="58TlvqhXRbr" role="3cqZAp">
          <node concept="d57v9" id="58TlvqhXRbs" role="3clFbG">
            <node concept="37vLTw" id="58TlvqhXRbt" role="37vLTJ">
              <ref role="3cqZAo" node="58TlvqhXRbo" resolve="date" />
            </node>
            <node concept="3cmrfG" id="58TlvqhXRbu" role="37vLTx">
              <property role="3cmrfH" value="1" />
            </node>
          </node>
        </node>
        <node concept="3clFbH" id="58TlvqhXRbv" role="3cqZAp" />
        <node concept="3clFbF" id="58TlvqhXRbw" role="3cqZAp">
          <node concept="2OqwBi" id="58TlvqhXRbx" role="3clFbG">
            <node concept="10M0yZ" id="58TlvqhXRby" role="2Oq$k0">
              <ref role="1PxDUh" to="wyt6:~System" resolve="System" />
              <ref role="3cqZAo" to="wyt6:~System.out" resolve="out" />
            </node>
            <node concept="liA8E" id="58TlvqhXRbz" role="2OqNvi">
              <ref role="37wK5l" to="guwi:~PrintStream.println(java.lang.Object)" resolve="println" />
              <node concept="3cpWs3" id="58TlvqhXRb$" role="37wK5m">
                <node concept="3cmrfG" id="58TlvqhXRb_" role="3uHU7w">
                  <property role="3cmrfH" value="1" />
                </node>
                <node concept="37vLTw" id="58TlvqhXRbA" role="3uHU7B">
                  <ref role="3cqZAo" node="58TlvqhXRbo" resolve="date" />
                </node>
              </node>
            </node>
          </node>
        </node>
      </node>
      <node concept="3cqZAl" id="58TlvqhXRbk" role="3clF45" />
      <node concept="3Tm1VV" id="58TlvqhXRbl" role="1B3o_S" />
    </node>
    <node concept="2tJIrI" id="58TlvqhXxdJ" role="jymVt" />
    <node concept="2YIFZL" id="58TlvqhXQjJ" role="jymVt">
      <property role="TrG5h" value="bar" />
      <node concept="3clFbS" id="58TlvqhXQjL" role="3clF47">
        <node concept="3cpWs8" id="58TlvqhXQjM" role="3cqZAp">
          <node concept="3cpWsn" id="58TlvqhXQjN" role="3cpWs9">
            <property role="TrG5h" value="stringBox" />
            <node concept="WRhlO" id="58TlvqhXQjO" role="1tU5fm">
              <node concept="17QB3L" id="4E9YLO0jdLk" role="WRhlP" />
            </node>
            <node concept="2ShNRf" id="58TlvqhXQjQ" role="33vP2m">
              <node concept="WRhv0" id="58TlvqhXQjR" role="2ShVmc">
                <node concept="Xl_RD" id="58TlvqhXQjS" role="WRhul">
                  <property role="Xl_RC" value="1" />
                </node>
                <node concept="17QB3L" id="4E9YLO0jeDP" role="1aNO72" />
              </node>
            </node>
          </node>
        </node>
        <node concept="3clFbF" id="58TlvqhXQjT" role="3cqZAp">
          <node concept="37vLTI" id="58TlvqhXQjU" role="3clFbG">
            <node concept="Xl_RD" id="58TlvqhXQjV" role="37vLTx">
              <property role="Xl_RC" value="2" />
            </node>
            <node concept="2OqwBi" id="58TlvqhXQjW" role="37vLTJ">
              <node concept="37vLTw" id="58TlvqhXQjX" role="2Oq$k0">
                <ref role="3cqZAo" node="58TlvqhXQjN" resolve="stringBox" />
              </node>
              <node concept="WRFkK" id="58TlvqhXQjY" role="2OqNvi" />
            </node>
          </node>
        </node>
        <node concept="3clFbF" id="58TlvqhXQjZ" role="3cqZAp">
          <node concept="d57v9" id="58TlvqhXQk0" role="3clFbG">
            <node concept="Xl_RD" id="58TlvqhXQk1" role="37vLTx">
              <property role="Xl_RC" value="1" />
            </node>
            <node concept="2OqwBi" id="58TlvqhXQk2" role="37vLTJ">
              <node concept="37vLTw" id="58TlvqhXQk3" role="2Oq$k0">
                <ref role="3cqZAo" node="58TlvqhXQjN" resolve="stringBox" />
              </node>
              <node concept="WRFkK" id="58TlvqhXQk4" role="2OqNvi" />
            </node>
          </node>
        </node>
        <node concept="3clFbH" id="58TlvqhXQk5" role="3cqZAp" />
        <node concept="3clFbF" id="58TlvqhXQk6" role="3cqZAp">
          <node concept="2OqwBi" id="58TlvqhXQk7" role="3clFbG">
            <node concept="10M0yZ" id="58TlvqhXQk8" role="2Oq$k0">
              <ref role="1PxDUh" to="wyt6:~System" resolve="System" />
              <ref role="3cqZAo" to="wyt6:~System.out" resolve="out" />
            </node>
            <node concept="liA8E" id="58TlvqhXQk9" role="2OqNvi">
              <ref role="37wK5l" to="guwi:~PrintStream.println(java.lang.String)" resolve="println" />
              <node concept="2OqwBi" id="58TlvqhXQka" role="37wK5m">
                <node concept="37vLTw" id="58TlvqhXQkb" role="2Oq$k0">
                  <ref role="3cqZAo" node="58TlvqhXQjN" resolve="stringBox" />
                </node>
                <node concept="WRFkK" id="58TlvqhXQkc" role="2OqNvi" />
              </node>
            </node>
          </node>
        </node>
      </node>
      <node concept="3cqZAl" id="58TlvqhXQke" role="3clF45" />
      <node concept="3Tm1VV" id="58TlvqhXQkd" role="1B3o_S" />
    </node>
    <node concept="2tJIrI" id="58TlvqhXEW2" role="jymVt" />
    <node concept="2YIFZL" id="58TlvqhXP_9" role="jymVt">
      <property role="TrG5h" value="baz" />
      <node concept="3clFbS" id="58TlvqhXP_d" role="3clF47">
        <node concept="3cpWs8" id="58TlvqhXP_e" role="3cqZAp">
          <node concept="3cpWsn" id="58TlvqhXP_f" role="3cpWs9">
            <property role="TrG5h" value="dateBox" />
            <node concept="WRhlO" id="58TlvqhXP_g" role="1tU5fm">
              <node concept="1Yu$k$" id="58TlvqhXP_h" role="WRhlP" />
            </node>
            <node concept="2ShNRf" id="58TlvqhXP_i" role="33vP2m">
              <node concept="WRhv0" id="58TlvqhXP_j" role="2ShVmc">
                <node concept="1Ypomk" id="58TlvqhXP_k" role="WRhul">
                  <property role="1Ypoml" value="2001-01-01" />
                </node>
                <node concept="1Yu$k$" id="4E9YLO0jbD_" role="1aNO72" />
              </node>
            </node>
          </node>
        </node>
        <node concept="3clFbF" id="58TlvqhXP_l" role="3cqZAp">
          <node concept="37vLTI" id="58TlvqhXP_m" role="3clFbG">
            <node concept="2OqwBi" id="58TlvqhXP_n" role="37vLTJ">
              <node concept="37vLTw" id="58TlvqhXP_o" role="2Oq$k0">
                <ref role="3cqZAo" node="58TlvqhXP_f" resolve="dateBox" />
              </node>
              <node concept="WRFkK" id="58TlvqhXP_p" role="2OqNvi" />
            </node>
            <node concept="1Ypomk" id="58TlvqhXP_q" role="37vLTx">
              <property role="1Ypoml" value="2001-01-02" />
            </node>
          </node>
        </node>
        <node concept="3clFbF" id="58TlvqhXP_r" role="3cqZAp">
          <node concept="d57v9" id="58TlvqhXP_s" role="3clFbG">
            <node concept="2OqwBi" id="58TlvqhXP_t" role="37vLTJ">
              <node concept="37vLTw" id="58TlvqhXP_u" role="2Oq$k0">
                <ref role="3cqZAo" node="58TlvqhXP_f" resolve="dateBox" />
              </node>
              <node concept="WRFkK" id="58TlvqhXP_v" role="2OqNvi" />
            </node>
            <node concept="3cmrfG" id="58TlvqhXP_w" role="37vLTx">
              <property role="3cmrfH" value="1" />
            </node>
          </node>
        </node>
        <node concept="3clFbH" id="58TlvqhXP_x" role="3cqZAp" />
        <node concept="3clFbF" id="58TlvqhXP_y" role="3cqZAp">
          <node concept="2OqwBi" id="58TlvqhXP_z" role="3clFbG">
            <node concept="10M0yZ" id="58TlvqhXP_$" role="2Oq$k0">
              <ref role="1PxDUh" to="wyt6:~System" resolve="System" />
              <ref role="3cqZAo" to="wyt6:~System.out" resolve="out" />
            </node>
            <node concept="liA8E" id="58TlvqhXP__" role="2OqNvi">
              <ref role="37wK5l" to="guwi:~PrintStream.println(java.lang.Object)" resolve="println" />
              <node concept="2OqwBi" id="58TlvqhXP_A" role="37wK5m">
                <node concept="37vLTw" id="58TlvqhXP_B" role="2Oq$k0">
                  <ref role="3cqZAo" node="58TlvqhXP_f" resolve="dateBox" />
                </node>
                <node concept="WRFkK" id="58TlvqhXP_C" role="2OqNvi" />
              </node>
            </node>
          </node>
        </node>
      </node>
      <node concept="3cqZAl" id="58TlvqhXP_b" role="3clF45" />
      <node concept="3Tm1VV" id="58TlvqhXP_c" role="1B3o_S" />
    </node>
    <node concept="2tJIrI" id="58TlvqhXOGt" role="jymVt" />
    <node concept="2YIFZL" id="58TlvqhXP8e" role="jymVt">
      <property role="TrG5h" value="main" />
      <node concept="37vLTG" id="58TlvqhXP8f" role="3clF46">
        <property role="TrG5h" value="args" />
        <node concept="10Q1$e" id="58TlvqhXP8g" role="1tU5fm">
          <node concept="17QB3L" id="58TlvqhXP8h" role="10Q1$1" />
        </node>
      </node>
      <node concept="3cqZAl" id="58TlvqhXP8i" role="3clF45" />
      <node concept="3Tm1VV" id="58TlvqhXP8j" role="1B3o_S" />
      <node concept="3clFbS" id="58TlvqhXP8k" role="3clF47">
        <node concept="3clFbF" id="58TlvqhXRSw" role="3cqZAp">
          <node concept="1rXfSq" id="58TlvqhXRSv" role="3clFbG">
            <ref role="37wK5l" node="58TlvqhXRbi" resolve="foo" />
          </node>
        </node>
        <node concept="3clFbF" id="58TlvqhXSeO" role="3cqZAp">
          <node concept="1rXfSq" id="58TlvqhXSeM" role="3clFbG">
            <ref role="37wK5l" node="58TlvqhXQjJ" resolve="bar" />
          </node>
        </node>
        <node concept="3clFbF" id="58TlvqhXSln" role="3cqZAp">
          <node concept="1rXfSq" id="58TlvqhXSll" role="3clFbG">
            <ref role="37wK5l" node="58TlvqhXP_9" resolve="baz" />
          </node>
        </node>
      </node>
    </node>
    <node concept="3Tm1VV" id="58TlvqhXvNu" role="1B3o_S" />
  </node>
</model>

