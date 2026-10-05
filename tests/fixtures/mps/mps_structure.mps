<?xml version="1.0" encoding="UTF-8"?>
<model ref="r:00000000-0000-4000-0000-011c89590292(jetbrains.mps.lang.structure.structure)">
  <persistence version="9" />
  <languages>
    <use id="982eb8df-2c96-4bd7-9963-11712ea622e5" name="jetbrains.mps.lang.resources" version="4" />
    <devkit ref="78434eb8-b0e5-444b-850d-e7c4ad2da9ab(jetbrains.mps.devkit.aspect.structure)" />
  </languages>
  <imports>
    <import index="tpck" ref="r:00000000-0000-4000-0000-011c89590288(jetbrains.mps.lang.core.structure)" />
    <import index="1oap" ref="r:03d44d4c-3d65-461c-9085-0f48e9569e59(jetbrains.mps.lang.resources.structure)" />
    <import index="tpcc" ref="r:00000000-0000-4000-0000-011c89590290(jetbrains.mps.lang.structure.plugin)" />
    <import index="yv2q" ref="r:cdf8afc0-fdc6-47ca-b829-7b2226168efa(jetbrains.mps.lang.descriptor.structure)" />
  </imports>
  <registry>
    <language id="982eb8df-2c96-4bd7-9963-11712ea622e5" name="jetbrains.mps.lang.resources">
      <concept id="5177162104569058199" name="jetbrains.mps.lang.resources.structure.HelpURL" flags="ng" index="1sEMCm">
        <property id="5177162104569058200" name="url" index="1sEMCp" />
        <reference id="4726480899534317252" name="baseURL" index="1fZFei" />
      </concept>
      <concept id="8974276187400029883" name="jetbrains.mps.lang.resources.structure.FileIcon" flags="ng" index="1QGGSu">
        <property id="2756621024541341363" name="file" index="1iqoE4" />
      </concept>
    </language>
    <language id="c72da2b9-7cce-4447-8389-f407dc1158b7" name="jetbrains.mps.lang.structure">
      <concept id="3348158742936976480" name="jetbrains.mps.lang.structure.structure.EnumerationMemberDeclaration" flags="ng" index="25R33">
        <property id="1421157252384165432" name="memberId" index="3tVfz5" />
        <property id="672037151186491528" name="presentation" index="1L1pqM" />
      </concept>
      <concept id="3348158742936976479" name="jetbrains.mps.lang.structure.structure.EnumerationDeclaration" flags="ng" index="25R3W">
        <reference id="1075010451642646892" name="defaultMember" index="1H5jkz" />
        <child id="3348158742936976577" name="members" index="25R1y" />
      </concept>
      <concept id="1224240836180" name="jetbrains.mps.lang.structure.structure.DeprecatedNodeAnnotation" flags="ig" index="asaX9">
        <property id="1225118929411" name="build" index="YLPcu" />
        <property id="1225118933224" name="comment" index="YLQ7P" />
      </concept>
      <concept id="7862711839422615209" name="jetbrains.mps.lang.structure.structure.DocumentedNodeAnnotation" flags="ng" index="t5JxF">
        <property id="7862711839422615217" name="text" index="t5JxN" />
      </concept>
      <concept id="6054523464626862044" name="jetbrains.mps.lang.structure.structure.AttributeInfo_IsMultiple" flags="ng" index="tn0Fv">
        <property id="6054523464626875854" name="value" index="tnX3d" />
      </concept>
      <concept id="6054523464627964745" name="jetbrains.mps.lang.structure.structure.AttributeInfo_AttributedConcept" flags="ng" index="trNpa">
        <reference id="6054523464627965081" name="concept" index="trN6q" />
      </concept>
      <concept id="1082978164218" name="jetbrains.mps.lang.structure.structure.DataTypeDeclaration" flags="ng" index="AxPO6">
        <property id="7791109065626895363" name="datatypeId" index="3F6X1D" />
      </concept>
      <concept id="1082978499127" name="jetbrains.mps.lang.structure.structure.ConstrainedDataTypeDeclaration" flags="ng" index="Az7Fb">
        <property id="1083066089218" name="constraint" index="FLfZY" />
      </concept>
      <concept id="2992811758677295509" name="jetbrains.mps.lang.structure.structure.AttributeInfo" flags="ng" index="M6xJ_">
        <property id="7588428831955550663" name="role" index="Hh88m" />
        <child id="7588428831947959310" name="attributed" index="EQaZv" />
        <child id="7588428831955550186" name="multiple" index="HhnKV" />
      </concept>
      <concept id="1169125787135" name="jetbrains.mps.lang.structure.structure.AbstractConceptDeclaration" flags="ig" index="PkWjJ">
        <property id="6714410169261853888" name="conceptId" index="EcuMT" />
        <property id="4628067390765907488" name="conceptShortDescription" index="R4oN_" />
        <property id="4628067390765956807" name="final" index="R5$K2" />
        <property id="4628067390765956802" name="abstract" index="R5$K7" />
        <property id="5092175715804935370" name="conceptAlias" index="34LRSv" />
        <child id="1780177113170204155" name="helpURL" index="bvy1s" />
        <child id="1071489727083" name="linkDeclaration" index="1TKVEi" />
        <child id="1071489727084" name="propertyDeclaration" index="1TKVEl" />
      </concept>
      <concept id="1169125989551" name="jetbrains.mps.lang.structure.structure.InterfaceConceptDeclaration" flags="ig" index="PlHQZ">
        <child id="1169127546356" name="extends" index="PrDN$" />
      </concept>
      <concept id="1169127622168" name="jetbrains.mps.lang.structure.structure.InterfaceConceptReference" flags="ig" index="PrWs8">
        <reference id="1169127628841" name="intfc" index="PrY4T" />
      </concept>
      <concept id="418049251856799813" name="jetbrains.mps.lang.structure.structure.ExperimentalAPINodeAttribute" flags="ig" index="1AhYRh" />
      <concept id="5583843313507852240" name="jetbrains.mps.lang.structure.structure.MarkerInterfaceAttribute" flags="ng" index="3MrDgS" />
      <concept id="1071489090640" name="jetbrains.mps.lang.structure.structure.ConceptDeclaration" flags="ig" index="1TIwiD">
        <property id="1096454100552" name="rootable" index="19KtqR" />
        <reference id="1071489389519" name="extends" index="1TJDcQ" />
        <child id="6327362524875300597" name="icon" index="rwd14" />
        <child id="1169129564478" name="implements" index="PzmwI" />
      </concept>
      <concept id="1071489288299" name="jetbrains.mps.lang.structure.structure.PropertyDeclaration" flags="ig" index="1TJgyi">
        <property id="241647608299431129" name="propertyId" index="IQ2nx" />
        <reference id="1082985295845" name="dataType" index="AX2Wp" />
      </concept>
      <concept id="1071489288298" name="jetbrains.mps.lang.structure.structure.LinkDeclaration" flags="ig" index="1TJgyj">
        <property id="1071599776563" name="role" index="20kJfa" />
        <property id="1071599893252" name="sourceCardinality" index="20lbJX" />
        <property id="1071599937831" name="metaClass" index="20lmBu" />
        <property id="241647608299431140" name="linkId" index="IQ2ns" />
        <reference id="1071599976176" name="target" index="20lvS9" />
      </concept>
    </language>
    <language id="ceab5195-25ea-4f22-9b92-103b95ca8c0c" name="jetbrains.mps.lang.core">
      <concept id="1133920641626" name="jetbrains.mps.lang.core.structure.BaseConcept" flags="ng" index="2VYdi">
        <property id="1193676396447" name="virtualPackage" index="3GE5qa" />
        <child id="5169995583184591170" name="smodelAttribute" index="lGtFl" />
      </concept>
      <concept id="1169194658468" name="jetbrains.mps.lang.core.structure.INamedConcept" flags="ngI" index="TrEIO">
        <property id="1169194664001" name="name" index="TrG5h" />
      </concept>
    </language>
  </registry>
  <node concept="1TIwiD" id="f_TJgxE">
    <property role="R5$K7" value="false" />
    <property role="R5$K2" value="true" />
    <property role="TrG5h" value="LinkDeclaration" />
    <property role="EcuMT" value="1071489288298" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="PrWs8" id="50$LP2ztBgg" role="PzmwI">
      <ref role="PrY4T" to="tpck:19gBtYEAf4C" resolve="InterfacePart" />
    </node>
    <node concept="1TJgyi" id="fA0kJcN" role="1TKVEl">
      <property role="TrG5h" value="role" />
      <property role="IQ2nx" value="1071599776563" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="3Ftr4R6BH8$" role="1TKVEl">
      <property role="TrG5h" value="metaClass" />
      <property role="IQ2nx" value="1071599937831" />
      <ref role="AX2Wp" node="3Ftr4R6BFyl" resolve="LinkMetaclass" />
    </node>
    <node concept="1TJgyi" id="3Ftr4R6BH8A" role="1TKVEl">
      <property role="TrG5h" value="sourceCardinality" />
      <property role="IQ2nx" value="1071599893252" />
      <ref role="AX2Wp" node="3Ftr4R6BFyf" resolve="Cardinality" />
    </node>
    <node concept="1TJgyi" id="24YP6ZDjW8M" role="1TKVEl">
      <property role="TrG5h" value="unordered" />
      <property role="IQ2nx" value="2395585628928459314" />
      <ref role="AX2Wp" to="tpck:fKAQMTB" resolve="boolean" />
    </node>
    <node concept="1TJgyi" id="dqwjwHwEj$" role="1TKVEl">
      <property role="TrG5h" value="linkId" />
      <property role="IQ2nx" value="241647608299431140" />
      <ref role="AX2Wp" node="3H1xM9LtLda" resolve="IDNumber" />
    </node>
    <node concept="1TJgyi" id="4B13ZO$O3Ox" role="1TKVEl">
      <property role="IQ2nx" value="5314546615192665377" />
      <property role="TrG5h" value="transient" />
      <ref role="AX2Wp" to="tpck:fKAQMTB" resolve="boolean" />
    </node>
    <node concept="1TJgyj" id="fA0ks94" role="1TKVEi">
      <property role="20kJfa" value="specializedLink" />
      <property role="IQ2ns" value="1071599698500" />
      <ref role="20lvS9" node="f_TJgxE" resolve="LinkDeclaration" />
    </node>
    <node concept="1TJgyj" id="fA0lvVK" role="1TKVEi">
      <property role="20kJfa" value="target" />
      <property role="20lbJX" value="fLJekj4/1" />
      <property role="IQ2ns" value="1071599976176" />
      <ref role="20lvS9" node="h0PkWnZ" resolve="AbstractConceptDeclaration" />
    </node>
    <node concept="PrWs8" id="hRkpyE_" role="PzmwI">
      <ref role="PrY4T" node="hOIDzy1" resolve="IStructureDeprecatable" />
    </node>
    <node concept="PrWs8" id="1ob16QT5sy7" role="PzmwI">
      <ref role="PrY4T" node="1ob16QT2yIl" resolve="INamedStructureElement" />
    </node>
    <node concept="PrWs8" id="6OtXG9KbAAy" role="PzmwI">
      <ref role="PrY4T" node="6OtXG9K2KUI" resolve="DocumentationObjective" />
    </node>
    <node concept="1QGGSu" id="4Q8sAA5iJRH" role="rwd14">
      <property role="1iqoE4" value="${module}/icons/link.png" />
    </node>
    <node concept="1sEMCm" id="12TOPj1JE4X" role="bvy1s">
      <property role="1sEMCp" value="structure.html#conceptmembers" />
      <ref role="1fZFei" to="tpcc:46nPloez0vX" resolve="HelpCenterDocUrl" />
    </node>
  </node>
  <node concept="1TIwiD" id="f_TJgxF">
    <property role="R5$K7" value="false" />
    <property role="R5$K2" value="true" />
    <property role="TrG5h" value="PropertyDeclaration" />
    <property role="EcuMT" value="1071489288299" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="1TJgyi" id="dqwjwHwEjp" role="1TKVEl">
      <property role="TrG5h" value="propertyId" />
      <property role="IQ2nx" value="241647608299431129" />
      <ref role="AX2Wp" node="3H1xM9LtLda" resolve="IDNumber" />
    </node>
    <node concept="1TJgyi" id="4B13ZO$O3x3" role="1TKVEl">
      <property role="IQ2nx" value="5314546615192664131" />
      <property role="TrG5h" value="transient" />
      <ref role="AX2Wp" to="tpck:fKAQMTB" resolve="boolean" />
    </node>
    <node concept="PrWs8" id="50$LP2ztBgn" role="PzmwI">
      <ref role="PrY4T" to="tpck:19gBtYEAf4C" resolve="InterfacePart" />
    </node>
    <node concept="PrWs8" id="1ob16QT4XKh" role="PzmwI">
      <ref role="PrY4T" node="1ob16QT2yIl" resolve="INamedStructureElement" />
    </node>
    <node concept="PrWs8" id="hOIDY1x" role="PzmwI">
      <ref role="PrY4T" node="hOIDzy1" resolve="IStructureDeprecatable" />
    </node>
    <node concept="PrWs8" id="6OtXG9KbA_V" role="PzmwI">
      <ref role="PrY4T" node="6OtXG9K2KUI" resolve="DocumentationObjective" />
    </node>
    <node concept="1TJgyj" id="fKAX2Z_" role="1TKVEi">
      <property role="20kJfa" value="dataType" />
      <property role="20lbJX" value="fLJekj4/1" />
      <property role="IQ2ns" value="1082985295845" />
      <ref role="20lvS9" node="fKAxPRU" resolve="DataTypeDeclaration" />
    </node>
    <node concept="1QGGSu" id="4Q8sAA5iJRJ" role="rwd14">
      <property role="1iqoE4" value="${module}/icons/property.png" />
    </node>
    <node concept="1sEMCm" id="12TOPj1JEfM" role="bvy1s">
      <property role="1sEMCp" value="structure.html#conceptmembers" />
      <ref role="1fZFei" to="tpcc:46nPloez0vX" resolve="HelpCenterDocUrl" />
    </node>
  </node>
  <node concept="1TIwiD" id="fKAxPRU">
    <property role="TrG5h" value="DataTypeDeclaration" />
    <property role="EcuMT" value="1082978164218" />
    <property role="R5$K7" value="true" />
    <node concept="1TJgyi" id="6Kv_6E714g3" role="1TKVEl">
      <property role="TrG5h" value="datatypeId" />
      <property role="IQ2nx" value="7791109065626895363" />
      <ref role="AX2Wp" node="3H1xM9LtLda" resolve="IDNumber" />
    </node>
    <node concept="1TJgyi" id="6Kv_6E714g4" role="1TKVEl">
      <property role="IQ2nx" value="7791109065626895364" />
      <property role="TrG5h" value="languageId" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="PrWs8" id="1ob16QT37Kd" role="PzmwI">
      <ref role="PrY4T" node="1ob16QT2yIl" resolve="INamedStructureElement" />
    </node>
    <node concept="PrWs8" id="5GuVNMLxi4Q" role="PzmwI">
      <ref role="PrY4T" node="hOIDzy1" resolve="IStructureDeprecatable" />
    </node>
    <node concept="PrWs8" id="7cLr0yQ8OvH" role="PzmwI">
      <ref role="PrY4T" node="6OtXG9K2KUI" resolve="DocumentationObjective" />
    </node>
  </node>
  <node concept="1TIwiD" id="fKAz7CR">
    <property role="19KtqR" value="true" />
    <property role="R5$K7" value="false" />
    <property role="R5$K2" value="false" />
    <property role="TrG5h" value="ConstrainedDataTypeDeclaration" />
    <property role="34LRSv" value="Constrained Data Type" />
    <property role="EcuMT" value="1082978499127" />
    <ref role="1TJDcQ" node="fKAxPRU" resolve="DataTypeDeclaration" />
    <node concept="1TJgyi" id="fKFLfW2" role="1TKVEl">
      <property role="TrG5h" value="constraint" />
      <property role="IQ2nx" value="1083066089218" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1QGGSu" id="4Q8sAA5iJRF" role="rwd14">
      <property role="1iqoE4" value="${module}/icons/constrainedTD.png" />
    </node>
    <node concept="1sEMCm" id="5Jed3OJ4_8g" role="bvy1s">
      <property role="1sEMCp" value="structure.html#constraineddatatypes" />
      <ref role="1fZFei" to="tpcc:46nPloez0vX" resolve="HelpCenterDocUrl" />
    </node>
  </node>
  <node concept="1TIwiD" id="fKM4N6M">
    <property role="TrG5h" value="EnumerationMemberDeclaration_Old" />
    <property role="EcuMT" value="1083171877298" />
    <property role="3GE5qa" value="enums.old" />
    <node concept="1TJgyi" id="fLuS6pz" role="1TKVEl">
      <property role="TrG5h" value="internalValue" />
      <property role="IQ2nx" value="1083923523171" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="fLuS6p$" role="1TKVEl">
      <property role="TrG5h" value="externalValue" />
      <property role="IQ2nx" value="1083923523172" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="hmfHohT" role="1TKVEl">
      <property role="TrG5h" value="javaIdentifier" />
      <property role="IQ2nx" value="1192116978809" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="PrWs8" id="1$21UiMc2k0" role="PzmwI">
      <ref role="PrY4T" to="tpck:19gBtYEAf4C" resolve="InterfacePart" />
    </node>
    <node concept="asaX9" id="VFd4X$dxFi" role="lGtFl">
      <property role="YLQ7P" value="Use EnumerationMemberDeclaration instead" />
      <property role="YLPcu" value="19.1" />
    </node>
  </node>
  <node concept="1TIwiD" id="fKQkHSB">
    <property role="19KtqR" value="true" />
    <property role="R5$K7" value="false" />
    <property role="R5$K2" value="true" />
    <property role="TrG5h" value="PrimitiveDataTypeDeclaration" />
    <property role="EcuMT" value="1083243159079" />
    <property role="34LRSv" value="Primitive Datatype" />
    <ref role="1TJDcQ" node="fKAxPRU" resolve="DataTypeDeclaration" />
    <node concept="1QGGSu" id="4Q8sAA5iJRL" role="rwd14">
      <property role="1iqoE4" value="${module}/icons/primitiveTD.png" />
    </node>
    <node concept="1sEMCm" id="5Jed3OJ4_9g" role="bvy1s">
      <property role="1sEMCp" value="structure.html#conceptmembers" />
      <ref role="1fZFei" to="tpcc:46nPloez0vX" resolve="HelpCenterDocUrl" />
    </node>
  </node>
  <node concept="1TIwiD" id="h0PkWnZ">
    <property role="R5$K7" value="true" />
    <property role="R5$K2" value="false" />
    <property role="TrG5h" value="AbstractConceptDeclaration" />
    <property role="EcuMT" value="1169125787135" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="1TJgyj" id="4mSE8vfOBGG" role="1TKVEi">
      <property role="IQ2ns" value="5023950685592517420" />
      <property role="20kJfa" value="sourceNode" />
      <ref role="20lvS9" to="tpck:gw2VY9q" resolve="BaseConcept" />
    </node>
    <node concept="1TJgyi" id="28RKVM5GRls" role="1TKVEl">
      <property role="TrG5h" value="oldHelpURL" />
      <property role="IQ2nx" value="2465654535473034588" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
      <node concept="asaX9" id="4S9dbA2RT1V" role="lGtFl">
        <property role="YLQ7P" value="use helpURL instead" />
        <property role="YLPcu" value="2017.3" />
      </node>
    </node>
    <node concept="1TJgyi" id="4qF2Hm2r7ja" role="1TKVEl">
      <property role="TrG5h" value="conceptAlias" />
      <property role="IQ2nx" value="5092175715804935370" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="40UcGlRaVSw" role="1TKVEl">
      <property role="TrG5h" value="conceptShortDescription" />
      <property role="IQ2nx" value="4628067390765907488" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="40UcGlRb7V2" role="1TKVEl">
      <property role="TrG5h" value="abstract" />
      <property role="IQ2nx" value="4628067390765956802" />
      <ref role="AX2Wp" to="tpck:fKAQMTB" resolve="boolean" />
    </node>
    <node concept="1TJgyi" id="40UcGlRb7V7" role="1TKVEl">
      <property role="TrG5h" value="final" />
      <property role="IQ2nx" value="4628067390765956807" />
      <ref role="AX2Wp" to="tpck:fKAQMTB" resolve="boolean" />
    </node>
    <node concept="1TJgyi" id="1o9qxtf9pgp" role="1TKVEl">
      <property role="TrG5h" value="intConceptId" />
      <property role="IQ2nx" value="1587916991969465369" />
      <ref role="AX2Wp" to="tpck:fKAQMTA" resolve="integer" />
      <node concept="asaX9" id="5OIo7_R7SMY" role="lGtFl" />
    </node>
    <node concept="1TJgyi" id="5OIo7_R7SN0" role="1TKVEl">
      <property role="TrG5h" value="conceptId" />
      <property role="IQ2nx" value="6714410169261853888" />
      <ref role="AX2Wp" node="3H1xM9LtLda" resolve="IDNumber" />
    </node>
    <node concept="1TJgyi" id="7NTi8jM8Rgz" role="1TKVEl">
      <property role="IQ2nx" value="9005308665739310115" />
      <property role="TrG5h" value="languageId" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="PrWs8" id="1ob16QT3ibT" role="PzmwI">
      <ref role="PrY4T" node="1ob16QT2yIl" resolve="INamedStructureElement" />
    </node>
    <node concept="PrWs8" id="50$LP2ztBbO" role="PzmwI">
      <ref role="PrY4T" to="tpck:19gBtYEAf4C" resolve="InterfacePart" />
    </node>
    <node concept="PrWs8" id="hOIE3lE" role="PzmwI">
      <ref role="PrY4T" node="hOIDzy1" resolve="IStructureDeprecatable" />
    </node>
    <node concept="PrWs8" id="1Bs_61$mdiU" role="PzmwI">
      <ref role="PrY4T" node="6OtXG9K2KUI" resolve="DocumentationObjective" />
    </node>
    <node concept="1TJgyj" id="f_TKVDF" role="1TKVEi">
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="linkDeclaration" />
      <property role="20lbJX" value="fLJekj5/0..n" />
      <property role="IQ2ns" value="1071489727083" />
      <ref role="20lvS9" node="f_TJgxE" resolve="LinkDeclaration" />
    </node>
    <node concept="1TJgyj" id="f_TKVDG" role="1TKVEi">
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="propertyDeclaration" />
      <property role="20lbJX" value="fLJekj5/0..n" />
      <property role="IQ2ns" value="1071489727084" />
      <ref role="20lvS9" node="f_TJgxF" resolve="PropertyDeclaration" />
    </node>
    <node concept="1TJgyj" id="1yOtqsjM_nV" role="1TKVEi">
      <property role="IQ2ns" value="1780177113170204155" />
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="helpURL" />
      <ref role="20lvS9" to="1oap:4voYo4rsXen" resolve="HelpURL" />
    </node>
  </node>
  <node concept="1TIwiD" id="h0PlHMJ">
    <property role="19KtqR" value="true" />
    <property role="R5$K7" value="false" />
    <property role="R5$K2" value="true" />
    <property role="TrG5h" value="InterfaceConceptDeclaration" />
    <property role="34LRSv" value="Interface Concept" />
    <property role="EcuMT" value="1169125989551" />
    <ref role="1TJDcQ" node="h0PkWnZ" resolve="AbstractConceptDeclaration" />
    <node concept="1TJgyj" id="h0PrDRO" role="1TKVEi">
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="extends" />
      <property role="20lbJX" value="fLJekj5/0..n" />
      <property role="IQ2ns" value="1169127546356" />
      <ref role="20lvS9" node="h0PrWoo" resolve="InterfaceConceptReference" />
    </node>
    <node concept="1QGGSu" id="4Q8sAA5iJRK" role="rwd14">
      <property role="1iqoE4" value="${module}/icons/interfaceStructure.png" />
    </node>
    <node concept="1sEMCm" id="12TOPj1JGOy" role="bvy1s">
      <property role="1sEMCp" value="structure.html#conceptsandconceptinterfaces" />
      <ref role="1fZFei" to="tpcc:46nPloez0vX" resolve="HelpCenterDocUrl" />
    </node>
  </node>
  <node concept="1TIwiD" id="h0PrWoo">
    <property role="TrG5h" value="InterfaceConceptReference" />
    <property role="EcuMT" value="1169127622168" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="1TJgyj" id="h0PrY0D" role="1TKVEi">
      <property role="20kJfa" value="intfc" />
      <property role="20lbJX" value="fLJekj4/1" />
      <property role="IQ2ns" value="1169127628841" />
      <ref role="20lvS9" node="h0PlHMJ" resolve="InterfaceConceptDeclaration" />
    </node>
    <node concept="PrWs8" id="50$LP2ztBnv" role="PzmwI">
      <ref role="PrY4T" to="tpck:19gBtYEAf4C" resolve="InterfacePart" />
    </node>
  </node>
  <node concept="1TIwiD" id="hOasaTk">
    <property role="TrG5h" value="DeprecatedNodeAnnotation" />
    <property role="3GE5qa" value="deprecatedAnnotation" />
    <property role="EcuMT" value="1224240836180" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="PrWs8" id="hOasgxt" role="PzmwI">
      <ref role="PrY4T" to="tpck:h0TrEE$" resolve="INamedConcept" />
    </node>
    <node concept="PrWs8" id="1PH8wrDG0$q" role="PzmwI">
      <ref role="PrY4T" to="tpck:19gBtYEAf4C" resolve="InterfacePart" />
    </node>
    <node concept="1TJgyi" id="hOYLP83" role="1TKVEl">
      <property role="TrG5h" value="build" />
      <property role="IQ2nx" value="1225118929411" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="hOYLQ3C" role="1TKVEl">
      <property role="TrG5h" value="comment" />
      <property role="IQ2nx" value="1225118933224" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="M6xJ_" id="6_gUeuqOePe" role="lGtFl">
      <property role="Hh88m" value="deprecatedNode" />
      <node concept="trNpa" id="166$sc$ZIAQ" role="EQaZv">
        <ref role="trN6q" node="hOIDzy1" resolve="IStructureDeprecatable" />
      </node>
    </node>
  </node>
  <node concept="PlHQZ" id="hOIDzy1">
    <property role="TrG5h" value="IStructureDeprecatable" />
    <property role="3GE5qa" value="deprecatedAnnotation" />
    <property role="EcuMT" value="1224848324737" />
    <node concept="PrWs8" id="hOIDCJM" role="PrDN$">
      <ref role="PrY4T" to="tpck:hOwnYed" resolve="IDeprecatable" />
    </node>
  </node>
  <node concept="PlHQZ" id="2hxg_BDjKM4">
    <property role="TrG5h" value="IConceptAspect" />
    <property role="EcuMT" value="2621449412040133764" />
    <node concept="3MrDgS" id="4m7OOLml7GU" role="lGtFl" />
  </node>
  <node concept="1TIwiD" id="2A8AB0r$C6l">
    <property role="TrG5h" value="AttributeInfo" />
    <property role="34LRSv" value="@attribute info" />
    <property role="3GE5qa" value="attribute" />
    <property role="EcuMT" value="2992811758677295509" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="1TJgyi" id="6_fwX542K77" role="1TKVEl">
      <property role="TrG5h" value="role" />
      <property role="IQ2nx" value="7588428831955550663" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyj" id="6_fwX542JZE" role="1TKVEi">
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="multiple" />
      <property role="IQ2ns" value="7588428831955550186" />
      <ref role="20lvS9" node="5g5Zgd9mvvs" resolve="AttributeInfo_IsMultiple" />
    </node>
    <node concept="1TJgyj" id="6_fwX53_MKe" role="1TKVEi">
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="attributed" />
      <property role="20lbJX" value="fLJekj5/0..n" />
      <property role="IQ2ns" value="7588428831947959310" />
      <ref role="20lvS9" node="5g5Zgd9qGH9" resolve="AttributeInfo_AttributedConcept" />
    </node>
    <node concept="M6xJ_" id="6_gUeuqOeTg" role="lGtFl">
      <property role="Hh88m" value="attributeInfo" />
      <node concept="tn0Fv" id="166$sc$NVlM" role="HhnKV">
        <property role="tnX3d" value="false" />
      </node>
      <node concept="trNpa" id="166$sc$ZIAG" role="EQaZv">
        <ref role="trN6q" node="f_TIwhg" resolve="ConceptDeclaration" />
      </node>
    </node>
  </node>
  <node concept="1TIwiD" id="5g5Zgd9mvvs">
    <property role="3GE5qa" value="attribute" />
    <property role="TrG5h" value="AttributeInfo_IsMultiple" />
    <property role="EcuMT" value="6054523464626862044" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="1TJgyi" id="5g5Zgd9myRe" role="1TKVEl">
      <property role="TrG5h" value="value" />
      <property role="IQ2nx" value="6054523464626875854" />
      <ref role="AX2Wp" to="tpck:fKAQMTB" resolve="boolean" />
    </node>
  </node>
  <node concept="1TIwiD" id="5g5Zgd9qGH9">
    <property role="3GE5qa" value="attribute" />
    <property role="TrG5h" value="AttributeInfo_AttributedConcept" />
    <property role="EcuMT" value="6054523464627964745" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="1TJgyj" id="5g5Zgd9qGMp" role="1TKVEi">
      <property role="20kJfa" value="concept" />
      <property role="20lbJX" value="fLJekj4/1" />
      <property role="IQ2ns" value="6054523464627965081" />
      <ref role="20lvS9" node="h0PkWnZ" resolve="AbstractConceptDeclaration" />
    </node>
  </node>
  <node concept="PlHQZ" id="4Xny4S7K8wS">
    <property role="TrG5h" value="ReferenceLinkDeclartionScopeKind" />
    <property role="3GE5qa" value="scope" />
    <property role="EcuMT" value="5717188120689018936" />
  </node>
  <node concept="PlHQZ" id="4Xny4S7K8CL">
    <property role="3GE5qa" value="scope" />
    <property role="TrG5h" value="AggregationLinkDeclarationScopeKind" />
    <property role="EcuMT" value="5717188120689019441" />
  </node>
  <node concept="Az7Fb" id="3H1xM9LtLda">
    <property role="TrG5h" value="IDNumber" />
    <property role="FLfZY" value="-?[0-9]+" />
    <property role="3F6X1D" value="4269842503726207818" />
  </node>
  <node concept="1TIwiD" id="6OtXG9K2KUP">
    <property role="EcuMT" value="7862711839422615221" />
    <property role="3GE5qa" value="document" />
    <property role="TrG5h" value="DocumentationObjectiveRef" />
    <property role="R4oN_" value="smart reference to documentable target" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="1TJgyj" id="6OtXG9K2KUQ" role="1TKVEi">
      <property role="IQ2ns" value="7862711839422615222" />
      <property role="20kJfa" value="target" />
      <property role="20lbJX" value="fLJekj4/1" />
      <ref role="20lvS9" node="6OtXG9K2KUI" resolve="DocumentationObjective" />
    </node>
  </node>
  <node concept="PlHQZ" id="6OtXG9K2KUI">
    <property role="EcuMT" value="7862711839422615214" />
    <property role="3GE5qa" value="document" />
    <property role="TrG5h" value="DocumentationObjective" />
  </node>
  <node concept="1TIwiD" id="6OtXG9K2KUD">
    <property role="EcuMT" value="7862711839422615209" />
    <property role="TrG5h" value="DocumentedNodeAnnotation" />
    <property role="3GE5qa" value="document" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="1TJgyj" id="6OtXG9K2KUS" role="1TKVEi">
      <property role="IQ2ns" value="7862711839422615224" />
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="seeAlso" />
      <property role="20lbJX" value="fLJekj5/0..n" />
      <ref role="20lvS9" node="6OtXG9K2KUP" resolve="DocumentationObjectiveRef" />
    </node>
    <node concept="M6xJ_" id="6OtXG9K2KUE" role="lGtFl">
      <property role="Hh88m" value="doc" />
      <node concept="tn0Fv" id="6OtXG9K2KUG" role="HhnKV">
        <property role="tnX3d" value="true" />
      </node>
      <node concept="trNpa" id="6OtXG9K2KUJ" role="EQaZv">
        <ref role="trN6q" node="6OtXG9K2KUI" resolve="DocumentationObjective" />
      </node>
    </node>
    <node concept="1TJgyi" id="6OtXG9K2KUL" role="1TKVEl">
      <property role="IQ2nx" value="7862711839422615217" />
      <property role="TrG5h" value="text" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
  </node>
  <node concept="1TIwiD" id="7ERGDLdoDvC">
    <property role="EcuMT" value="8842732777748207592" />
    <property role="TrG5h" value="SmartReferenceAttribute" />
    <property role="34LRSv" value="@smart reference" />
    <property role="3GE5qa" value="smartReference" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="1TJgyj" id="7ERGDLdoDvH" role="1TKVEi">
      <property role="IQ2ns" value="8842732777748207597" />
      <property role="20kJfa" value="charactersticReference" />
      <property role="20lbJX" value="fLJekj4/1" />
      <ref role="20lvS9" node="f_TJgxE" resolve="LinkDeclaration" />
    </node>
    <node concept="M6xJ_" id="7ERGDLdoDvD" role="lGtFl">
      <property role="Hh88m" value="smartReference" />
      <node concept="trNpa" id="7ERGDLdoDvF" role="EQaZv">
        <ref role="trN6q" node="f_TIwhg" resolve="ConceptDeclaration" />
      </node>
    </node>
    <node concept="1TJgyj" id="7ERGDLdpEKR" role="1TKVEi">
      <property role="IQ2ns" value="8842732777748474935" />
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="refPresentationTemplate" />
      <ref role="20lvS9" node="7ERGDLdpClu" resolve="RefPresentationTemplate" />
    </node>
  </node>
  <node concept="1TIwiD" id="7ERGDLdpClu">
    <property role="EcuMT" value="8842732777748464990" />
    <property role="3GE5qa" value="smartReference" />
    <property role="TrG5h" value="RefPresentationTemplate" />
    <property role="34LRSv" value="template" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="1TJgyi" id="3J8eUL7n4Hp" role="1TKVEl">
      <property role="IQ2nx" value="4307758654697524057" />
      <property role="TrG5h" value="prefix" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="3J8eUL7n4Hs" role="1TKVEl">
      <property role="IQ2nx" value="4307758654697524060" />
      <property role="TrG5h" value="suffix" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
  </node>
  <node concept="1TIwiD" id="fKAxPRV">
    <property role="R5$K7" value="false" />
    <property role="R5$K2" value="false" />
    <property role="TrG5h" value="EnumerationDataTypeDeclaration_Old" />
    <property role="34LRSv" value="Enum Data Type" />
    <property role="EcuMT" value="1082978164219" />
    <property role="3GE5qa" value="enums.old" />
    <ref role="1TJDcQ" node="fKAxPRU" resolve="DataTypeDeclaration" />
    <node concept="1TJgyi" id="3Ftr4R6BH8C" role="1TKVEl">
      <property role="TrG5h" value="memberIdentifierPolicy" />
      <property role="IQ2nx" value="1197591154882" />
      <ref role="AX2Wp" node="3Ftr4R6BFyp" resolve="EnumerationMemberIdentifierPolicy" />
    </node>
    <node concept="1TJgyi" id="hCPDuZq" role="1TKVEl">
      <property role="TrG5h" value="hasNoDefaultMember" />
      <property role="IQ2nx" value="1212080844762" />
      <ref role="AX2Wp" to="tpck:fKAQMTB" resolve="boolean" />
    </node>
    <node concept="1TJgyi" id="hCQ2FqA" role="1TKVEl">
      <property role="TrG5h" value="noValueText" />
      <property role="IQ2nx" value="1212087449254" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyj" id="fKM4eW5" role="1TKVEi">
      <property role="20kJfa" value="memberDataType" />
      <property role="20lbJX" value="fLJekj4/1" />
      <property role="IQ2ns" value="1083171729157" />
      <ref role="20lvS9" node="fKQkHSB" resolve="PrimitiveDataTypeDeclaration" />
    </node>
    <node concept="1TJgyj" id="fKQgatX" role="1TKVEi">
      <property role="20kJfa" value="defaultMember" />
      <property role="IQ2ns" value="1083241965437" />
      <ref role="20lvS9" node="fKM4N6M" resolve="EnumerationMemberDeclaration_Old" />
    </node>
    <node concept="1TJgyj" id="fKM5hVY" role="1TKVEi">
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="member" />
      <property role="20lbJX" value="fLJekj6/1..n" />
      <property role="IQ2ns" value="1083172003582" />
      <ref role="20lvS9" node="fKM4N6M" resolve="EnumerationMemberDeclaration_Old" />
    </node>
    <node concept="PrWs8" id="VFd4X$DAKZ" role="PzmwI">
      <ref role="PrY4T" node="VFd4X$DyB_" resolve="IEnumeration" />
    </node>
    <node concept="1QGGSu" id="4Q8sAA5iJRG" role="rwd14">
      <property role="1iqoE4" value="${module}/icons/enum.png" />
    </node>
    <node concept="asaX9" id="VFd4X$dxBi" role="lGtFl">
      <property role="YLPcu" value="19.1" />
      <property role="YLQ7P" value="Use EnumerationDeclaration instead" />
    </node>
  </node>
  <node concept="1TIwiD" id="f_TIwhg">
    <property role="R5$K7" value="false" />
    <property role="19KtqR" value="true" />
    <property role="R5$K2" value="true" />
    <property role="TrG5h" value="ConceptDeclaration" />
    <property role="34LRSv" value="Concept" />
    <property role="EcuMT" value="1071489090640" />
    <ref role="1TJDcQ" node="h0PkWnZ" resolve="AbstractConceptDeclaration" />
    <node concept="PrWs8" id="1YcAZC571At" role="PzmwI">
      <ref role="PrY4T" to="tpck:69Qfsw3InJo" resolve="ISmartReferent" />
    </node>
    <node concept="1TJgyi" id="fX9Ktp8" role="1TKVEl">
      <property role="TrG5h" value="rootable" />
      <property role="IQ2nx" value="1096454100552" />
      <ref role="AX2Wp" to="tpck:fKAQMTB" resolve="boolean" />
    </node>
    <node concept="1TJgyi" id="gSMwhzt" role="1TKVEl">
      <property role="TrG5h" value="iconPath" />
      <property role="IQ2nx" value="1160488491229" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
      <node concept="asaX9" id="4qBHWh_rs8W" role="lGtFl" />
    </node>
    <node concept="1TJgyi" id="3Ftr4R6BH8E" role="1TKVEl">
      <property role="TrG5h" value="staticScope" />
      <property role="IQ2nx" value="5404671619616246344" />
      <ref role="AX2Wp" node="3Ftr4R6BF$w" resolve="StaticScope" />
    </node>
    <node concept="1TJgyj" id="f_TJDff" role="1TKVEi">
      <property role="20kJfa" value="extends" />
      <property role="IQ2ns" value="1071489389519" />
      <ref role="20lvS9" node="f_TIwhg" resolve="ConceptDeclaration" />
    </node>
    <node concept="1TJgyj" id="h0Pzm$Y" role="1TKVEi">
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="implements" />
      <property role="20lbJX" value="fLJekj5/0..n" />
      <property role="IQ2ns" value="1169129564478" />
      <ref role="20lvS9" node="h0PrWoo" resolve="InterfaceConceptReference" />
    </node>
    <node concept="1TJgyj" id="5vfjF5cjTVP" role="1TKVEi">
      <property role="IQ2ns" value="6327362524875300597" />
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="icon" />
      <ref role="20lvS9" to="1oap:2p1v3tOa9VI" resolve="Icon" />
    </node>
    <node concept="1QGGSu" id="4Q8sAA5iJRI" role="rwd14">
      <property role="1iqoE4" value="${module}/icons/structure.png" />
    </node>
    <node concept="1sEMCm" id="11Atn_WK8fg" role="bvy1s">
      <property role="1sEMCp" value="structure.html#conceptsandconceptinterfaces" />
      <ref role="1fZFei" to="tpcc:46nPloez0vX" resolve="HelpCenterDocUrl" />
    </node>
  </node>
  <node concept="1TIwiD" id="2TR3acGo7Lv">
    <property role="EcuMT" value="3348158742936976479" />
    <property role="TrG5h" value="EnumerationDeclaration" />
    <property role="34LRSv" value="Enumeration" />
    <property role="3GE5qa" value="enums" />
    <property role="19KtqR" value="true" />
    <property role="R5$K2" value="true" />
    <ref role="1TJDcQ" node="fKAxPRU" resolve="DataTypeDeclaration" />
    <node concept="PrWs8" id="3nu5mKQenOr" role="PzmwI">
      <ref role="PrY4T" node="6OtXG9K2KUI" resolve="DocumentationObjective" />
    </node>
    <node concept="1TJgyj" id="2TR3acGo7N1" role="1TKVEi">
      <property role="IQ2ns" value="3348158742936976577" />
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="members" />
      <property role="20lbJX" value="fLJekj6/1..n" />
      <ref role="20lvS9" node="2TR3acGo7Lw" resolve="EnumerationMemberDeclaration" />
    </node>
    <node concept="1TJgyj" id="VFd4XzZw5G" role="1TKVEi">
      <property role="IQ2ns" value="1075010451642646892" />
      <property role="20kJfa" value="defaultMember" />
      <ref role="20lvS9" node="2TR3acGo7Lw" resolve="EnumerationMemberDeclaration" />
    </node>
    <node concept="PrWs8" id="VFd4X$DyUD" role="PzmwI">
      <ref role="PrY4T" node="VFd4X$DyB_" resolve="IEnumeration" />
    </node>
    <node concept="1QGGSu" id="VFd4X$JQVc" role="rwd14">
      <property role="1iqoE4" value="${module}/icons/enum.png" />
    </node>
    <node concept="1sEMCm" id="5Jed3OJ4_8k" role="bvy1s">
      <property role="1sEMCp" value="structure.html#enumerationds" />
      <ref role="1fZFei" to="tpcc:46nPloez0vX" resolve="HelpCenterDocUrl" />
    </node>
  </node>
  <node concept="1TIwiD" id="2TR3acGo7Lw">
    <property role="EcuMT" value="3348158742936976480" />
    <property role="3GE5qa" value="enums" />
    <property role="TrG5h" value="EnumerationMemberDeclaration" />
    <property role="34LRSv" value="member" />
    <property role="R5$K2" value="true" />
    <node concept="PrWs8" id="1ob16QT3iN4" role="PzmwI">
      <ref role="PrY4T" node="1ob16QT2yIl" resolve="INamedStructureElement" />
    </node>
    <node concept="PrWs8" id="7cLr0yQ8OoL" role="PzmwI">
      <ref role="PrY4T" node="6OtXG9K2KUI" resolve="DocumentationObjective" />
    </node>
    <node concept="1TJgyi" id="_jzzDSlxy8" role="1TKVEl">
      <property role="IQ2nx" value="672037151186491528" />
      <property role="TrG5h" value="presentation" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="1eSXJRel0SS" role="1TKVEl">
      <property role="IQ2nx" value="1421157252384165432" />
      <property role="TrG5h" value="memberId" />
      <ref role="AX2Wp" node="3H1xM9LtLda" resolve="IDNumber" />
    </node>
    <node concept="1TJgyj" id="LU8F6G7H5v" role="1TKVEi">
      <property role="IQ2ns" value="899069222106091871" />
      <property role="20kJfa" value="oldMember" />
      <ref role="20lvS9" node="fKM4N6M" resolve="EnumerationMemberDeclaration_Old" />
      <node concept="asaX9" id="LU8F6G7H6r" role="lGtFl">
        <property role="YLQ7P" value="For internal usage only" />
        <property role="YLPcu" value="19.1" />
      </node>
    </node>
  </node>
  <node concept="PlHQZ" id="VFd4X$DyB_">
    <property role="EcuMT" value="1075010451653667301" />
    <property role="3GE5qa" value="enums" />
    <property role="TrG5h" value="IEnumeration" />
    <node concept="PrWs8" id="50Pb80HvPH9" role="PrDN$">
      <ref role="PrY4T" to="tpck:h0TrEE$" resolve="INamedConcept" />
    </node>
    <node concept="asaX9" id="25DDrfqCETY" role="lGtFl">
      <property role="YLPcu" value="19.1" />
      <property role="YLQ7P" value="For internal usage only" />
    </node>
  </node>
  <node concept="PlHQZ" id="1ob16QT2yIl">
    <property role="EcuMT" value="1588368162880629653" />
    <property role="TrG5h" value="INamedStructureElement" />
    <node concept="PrWs8" id="1ob16QT2yIm" role="PrDN$">
      <ref role="PrY4T" to="tpck:h0TrEE$" resolve="INamedConcept" />
    </node>
    <node concept="PrWs8" id="1ob16QT2Prz" role="PrDN$">
      <ref role="PrY4T" node="1ob16QT2Pru" resolve="IStructureElement" />
    </node>
  </node>
  <node concept="PlHQZ" id="1ob16QT2Pru">
    <property role="EcuMT" value="1588368162880706270" />
    <property role="TrG5h" value="IStructureElement" />
  </node>
  <node concept="1TIwiD" id="1ob16QTis9A">
    <property role="EcuMT" value="1588368162884797030" />
    <property role="3GE5qa" value="enums.migration" />
    <property role="TrG5h" value="EnumMigrationInfo" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="1TJgyj" id="5CkWgdpgBAs" role="1TKVEi">
      <property role="IQ2ns" value="6491077959632451996" />
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="oldEnum" />
      <property role="20lbJX" value="fLJekj4/1" />
      <ref role="20lvS9" node="fKAxPRV" resolve="EnumerationDataTypeDeclaration_Old" />
    </node>
    <node concept="1TJgyi" id="5CkWgdpp0oI" role="1TKVEl">
      <property role="IQ2nx" value="6491077959634650670" />
      <property role="TrG5h" value="nameOpMigration" />
      <ref role="AX2Wp" node="5CkWgdpp0p0" resolve="NameOperationMigrationStrategy" />
    </node>
    <node concept="1TJgyi" id="5CkWgdpp3f$" role="1TKVEl">
      <property role="IQ2nx" value="6491077959634662372" />
      <property role="TrG5h" value="valueOpMigration" />
      <ref role="AX2Wp" node="5CkWgdpp3eX" resolve="ValueOperationMigrationStrategy" />
    </node>
    <node concept="M6xJ_" id="1ob16QTis9B" role="lGtFl">
      <property role="Hh88m" value="enumMigration" />
      <node concept="trNpa" id="1ob16QTis9F" role="EQaZv">
        <ref role="trN6q" node="2TR3acGo7Lv" resolve="EnumerationDeclaration" />
      </node>
      <node concept="tn0Fv" id="1ob16QTis9I" role="HhnKV" />
    </node>
    <node concept="PrWs8" id="5CkWgdpQ2KU" role="PzmwI">
      <ref role="PrY4T" to="tpck:2WmWrdnSpX3" resolve="ISuppressErrors" />
    </node>
  </node>
  <node concept="1TIwiD" id="5CkWgdpgEmF">
    <property role="EcuMT" value="6491077959632463275" />
    <property role="3GE5qa" value="enums.migration" />
    <property role="TrG5h" value="EnumPropertyMigrationInfo" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="1TJgyj" id="5CkWgdpgEmQ" role="1TKVEi">
      <property role="IQ2ns" value="6491077959632463286" />
      <property role="20lmBu" value="fLJjDmT/aggregation" />
      <property role="20kJfa" value="oldProperty" />
      <property role="20lbJX" value="fLJekj4/1" />
      <ref role="20lvS9" node="f_TJgxF" resolve="PropertyDeclaration" />
    </node>
    <node concept="M6xJ_" id="5CkWgdpgEmG" role="lGtFl">
      <property role="Hh88m" value="enumPropertyMigration" />
      <node concept="tn0Fv" id="5CkWgdpgEmI" role="HhnKV" />
      <node concept="trNpa" id="5CkWgdpgEmM" role="EQaZv">
        <ref role="trN6q" node="f_TJgxF" resolve="PropertyDeclaration" />
      </node>
    </node>
    <node concept="PrWs8" id="LU8F6Gpbee" role="PzmwI">
      <ref role="PrY4T" to="tpck:2WmWrdnSpX3" resolve="ISuppressErrors" />
    </node>
  </node>
  <node concept="25R3W" id="5CkWgdpp0p0">
    <property role="3GE5qa" value="enums.migration" />
    <property role="TrG5h" value="NameOperationMigrationStrategy" />
    <property role="3F6X1D" value="6491077959634650688" />
    <node concept="25R33" id="5CkWgdpp0p1" role="25R1y">
      <property role="TrG5h" value="by_name" />
      <property role="3tVfz5" value="6491077959634650689" />
    </node>
    <node concept="25R33" id="5CkWgdpp0p2" role="25R1y">
      <property role="TrG5h" value="by_presentation" />
      <property role="3tVfz5" value="6491077959634650690" />
    </node>
    <node concept="25R33" id="5CkWgdpp0p5" role="25R1y">
      <property role="TrG5h" value="by_custom_methods" />
      <property role="3tVfz5" value="6491077959634650693" />
    </node>
  </node>
  <node concept="25R3W" id="5CkWgdpp3eX">
    <property role="3GE5qa" value="enums.migration" />
    <property role="TrG5h" value="ValueOperationMigrationStrategy" />
    <property role="3F6X1D" value="6491077959634662333" />
    <node concept="25R33" id="5CkWgdpp3eY" role="25R1y">
      <property role="TrG5h" value="string_name" />
      <property role="3tVfz5" value="6491077959634662334" />
    </node>
    <node concept="25R33" id="5CkWgdpp3eZ" role="25R1y">
      <property role="TrG5h" value="string_presentation" />
      <property role="3tVfz5" value="6491077959634662335" />
    </node>
    <node concept="25R33" id="5CkWgdpp3f6" role="25R1y">
      <property role="TrG5h" value="boolean" />
      <property role="3tVfz5" value="6491077959634662342" />
    </node>
    <node concept="25R33" id="5CkWgdpp3fb" role="25R1y">
      <property role="TrG5h" value="int_ordinal" />
      <property role="3tVfz5" value="6491077959634662347" />
    </node>
    <node concept="25R33" id="5CkWgdpp3fh" role="25R1y">
      <property role="TrG5h" value="int_ordinal_plus_one" />
      <property role="3tVfz5" value="6491077959634662353" />
    </node>
    <node concept="25R33" id="5CkWgdpp3fo" role="25R1y">
      <property role="TrG5h" value="by_custom_methods" />
      <property role="3tVfz5" value="6491077959634662360" />
    </node>
  </node>
  <node concept="25R3W" id="2UidVq6Wu$P">
    <property role="3GE5qa" value="enums.migration" />
    <property role="TrG5h" value="EnumCustomMethodReplacementKind" />
    <property role="3F6X1D" value="3355805929432017205" />
    <node concept="25R33" id="2UidVq6Wu$Q" role="25R1y">
      <property role="TrG5h" value="memberToValue" />
      <property role="1L1pqM" value="member -&gt; value" />
      <property role="3tVfz5" value="3355805929432017206" />
    </node>
    <node concept="25R33" id="2UidVq6Wu$R" role="25R1y">
      <property role="TrG5h" value="valueToMember" />
      <property role="1L1pqM" value="value -&gt; member" />
      <property role="3tVfz5" value="3355805929432017207" />
    </node>
    <node concept="25R33" id="2UidVq6Wu$U" role="25R1y">
      <property role="TrG5h" value="memberToName" />
      <property role="1L1pqM" value="name -&gt; member" />
      <property role="3tVfz5" value="3355805929432017210" />
    </node>
    <node concept="25R33" id="2UidVq6Wu$Y" role="25R1y">
      <property role="TrG5h" value="nameToMember" />
      <property role="1L1pqM" value="member -&gt; name" />
      <property role="3tVfz5" value="3355805929432017214" />
    </node>
  </node>
  <node concept="1TIwiD" id="2UidVq6Wu_3">
    <property role="EcuMT" value="3355805929432017219" />
    <property role="3GE5qa" value="enums.migration" />
    <property role="TrG5h" value="EnumCustomMethodReplacementInfo" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="1TJgyj" id="2UidVq6Wu_8" role="1TKVEi">
      <property role="IQ2ns" value="3355805929432017224" />
      <property role="20kJfa" value="enum" />
      <property role="20lbJX" value="fLJekj4/1" />
      <ref role="20lvS9" node="2TR3acGo7Lv" resolve="EnumerationDeclaration" />
    </node>
    <node concept="M6xJ_" id="2UidVq6Wu_4" role="lGtFl">
      <property role="Hh88m" value="enumCustomMethodReplacement" />
      <node concept="trNpa" id="2UidVq6ZVx6" role="EQaZv">
        <ref role="trN6q" to="tpck:gw2VY9q" resolve="BaseConcept" />
      </node>
    </node>
    <node concept="1TJgyi" id="2UidVq6Wu_6" role="1TKVEl">
      <property role="IQ2nx" value="3355805929432017222" />
      <property role="TrG5h" value="kind" />
      <ref role="AX2Wp" node="2UidVq6Wu$P" resolve="EnumCustomMethodReplacementKind" />
    </node>
  </node>
  <node concept="25R3W" id="3Ftr4R6BFyf">
    <property role="TrG5h" value="Cardinality" />
    <property role="3F6X1D" value="1084197782722" />
    <ref role="1H5jkz" node="3Ftr4R6BFyh" resolve="_0__1" />
    <node concept="25R33" id="3Ftr4R6BFyh" role="25R1y">
      <property role="TrG5h" value="_0__1" />
      <property role="1L1pqM" value="0..1" />
      <property role="3tVfz5" value="1084197782723" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFyi" role="25R1y">
      <property role="TrG5h" value="_1" />
      <property role="1L1pqM" value="1" />
      <property role="3tVfz5" value="1084197782724" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFyj" role="25R1y">
      <property role="TrG5h" value="_0__n" />
      <property role="1L1pqM" value="0..n" />
      <property role="3tVfz5" value="1084197782725" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFyk" role="25R1y">
      <property role="TrG5h" value="_1__n" />
      <property role="1L1pqM" value="1..n" />
      <property role="3tVfz5" value="1084197782726" />
    </node>
  </node>
  <node concept="25R3W" id="3Ftr4R6BFyl">
    <property role="TrG5h" value="LinkMetaclass" />
    <property role="3F6X1D" value="1084199179703" />
    <ref role="1H5jkz" node="3Ftr4R6BFyn" resolve="reference" />
    <node concept="25R33" id="3Ftr4R6BFyn" role="25R1y">
      <property role="TrG5h" value="reference" />
      <property role="3tVfz5" value="1084199179704" />
      <property role="1L1pqM" value="association" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFyo" role="25R1y">
      <property role="TrG5h" value="aggregation" />
      <property role="3tVfz5" value="1084199179705" />
      <property role="1L1pqM" value="aggregation" />
    </node>
  </node>
  <node concept="25R3W" id="3Ftr4R6BFyp">
    <property role="TrG5h" value="EnumerationMemberIdentifierPolicy" />
    <property role="3F6X1D" value="1197590884613" />
    <ref role="1H5jkz" node="3Ftr4R6BFyr" resolve="derive_from_presentation" />
    <node concept="25R33" id="3Ftr4R6BFyr" role="25R1y">
      <property role="TrG5h" value="derive_from_presentation" />
      <property role="1L1pqM" value="derive from presentation" />
      <property role="3tVfz5" value="1197590884614" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFys" role="25R1y">
      <property role="TrG5h" value="derive_from_internal_value" />
      <property role="1L1pqM" value="derive from internal value" />
      <property role="3tVfz5" value="1197591048630" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFyt" role="25R1y">
      <property role="TrG5h" value="custom" />
      <property role="3tVfz5" value="1197591075491" />
    </node>
  </node>
  <node concept="25R3W" id="3Ftr4R6BFyu">
    <property role="TrG5h" value="InstanceIncomingReferencesPolicy" />
    <property role="3F6X1D" value="8087047305080774904" />
    <ref role="1H5jkz" node="3Ftr4R6BFyw" resolve="allowed" />
    <node concept="25R33" id="3Ftr4R6BFyw" role="25R1y">
      <property role="TrG5h" value="allowed" />
      <property role="3tVfz5" value="8087047305080775282" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFyx" role="25R1y">
      <property role="TrG5h" value="local" />
      <property role="1L1pqM" value="same root only" />
      <property role="3tVfz5" value="8087047305080775412" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFyy" role="25R1y">
      <property role="TrG5h" value="forbidden" />
      <property role="3tVfz5" value="8087047305080775416" />
    </node>
  </node>
  <node concept="25R3W" id="3Ftr4R6BFzv">
    <property role="TrG5h" value="ChildrenIncomingReferencesPolicy" />
    <property role="3F6X1D" value="3220559764717766993" />
    <ref role="1H5jkz" node="3Ftr4R6BFzx" resolve="allowed" />
    <node concept="25R33" id="3Ftr4R6BFzx" role="25R1y">
      <property role="TrG5h" value="allowed" />
      <property role="3tVfz5" value="3220559764717766994" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFzy" role="25R1y">
      <property role="TrG5h" value="local" />
      <property role="1L1pqM" value="enforce `same root only'" />
      <property role="3tVfz5" value="3220559764717766995" />
    </node>
    <node concept="25R33" id="3Ftr4R6BFzz" role="25R1y">
      <property role="TrG5h" value="forbidden" />
      <property role="1L1pqM" value="enforce `forbidden'" />
      <property role="3tVfz5" value="3220559764717766998" />
    </node>
  </node>
  <node concept="25R3W" id="3Ftr4R6BF$w">
    <property role="TrG5h" value="StaticScope" />
    <property role="3F6X1D" value="5404671619616246759" />
    <ref role="1H5jkz" node="3Ftr4R6BF$y" resolve="global" />
    <node concept="25R33" id="3Ftr4R6BF$y" role="25R1y">
      <property role="TrG5h" value="global" />
      <property role="3tVfz5" value="5404671619616246760" />
    </node>
    <node concept="25R33" id="3Ftr4R6BF$z" role="25R1y">
      <property role="TrG5h" value="root" />
      <property role="1L1pqM" value="containing root" />
      <property role="3tVfz5" value="5404671619616246761" />
    </node>
    <node concept="25R33" id="3Ftr4R6BF$$" role="25R1y">
      <property role="TrG5h" value="none" />
      <property role="3tVfz5" value="5404671619616246764" />
    </node>
  </node>
  <node concept="1TIwiD" id="nddphzyHx5">
    <property role="TrG5h" value="ExperimentalAPINodeAttribute" />
    <property role="3GE5qa" value="deprecatedAnnotation" />
    <property role="EcuMT" value="418049251856799813" />
    <property role="R5$K2" value="true" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="PrWs8" id="nddphzyHx6" role="PzmwI">
      <ref role="PrY4T" to="tpck:h0TrEE$" resolve="INamedConcept" />
    </node>
    <node concept="PrWs8" id="nddphzyHx7" role="PzmwI">
      <ref role="PrY4T" to="tpck:19gBtYEAf4C" resolve="InterfacePart" />
    </node>
    <node concept="1TJgyi" id="nddphzyHx8" role="1TKVEl">
      <property role="TrG5h" value="build" />
      <property role="IQ2nx" value="418049251856799816" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="1TJgyi" id="nddphzyHx9" role="1TKVEl">
      <property role="TrG5h" value="comment" />
      <property role="IQ2nx" value="418049251856799817" />
      <ref role="AX2Wp" to="tpck:fKAOsGN" resolve="string" />
    </node>
    <node concept="M6xJ_" id="nddphzyHxa" role="lGtFl">
      <property role="Hh88m" value="experimentalAPIAttribute" />
      <node concept="trNpa" id="nddphzEnSj" role="EQaZv">
        <ref role="trN6q" node="h0PkWnZ" resolve="AbstractConceptDeclaration" />
      </node>
    </node>
    <node concept="t5JxF" id="6WJr2NngPmT" role="lGtFl">
      <property role="t5JxN" value="Use this attribute with those concept declarations which offer some non-stable or early-access functionality" />
    </node>
  </node>
  <node concept="PlHQZ" id="6TyNL3imAnw">
    <property role="EcuMT" value="7954147563045283296" />
    <property role="TrG5h" value="INamedAspect" />
    <node concept="PrWs8" id="6TyNL3imAB9" role="PrDN$">
      <ref role="PrY4T" to="tpck:h0TrEE$" resolve="INamedConcept" />
    </node>
    <node concept="t5JxF" id="6TyNL3imB7E" role="lGtFl">
      <property role="t5JxN" value="defines a naming policy that all language aspects are recommended to use. May include spaces and other symbols in future. A correct way to generate a unique valid class name from it is now genContext.name from (this.getValidClassName()). It's better to create a generator language feature for that." />
    </node>
    <node concept="3MrDgS" id="IV8h_9QsUz" role="lGtFl" />
  </node>
  <node concept="1TIwiD" id="4PXMYyZ4PZg">
    <property role="EcuMT" value="5583843313507852240" />
    <property role="TrG5h" value="MarkerInterfaceAttribute" />
    <property role="34LRSv" value="@marker interface" />
    <ref role="1TJDcQ" to="tpck:2ULFgo8_XDk" resolve="NodeAttribute" />
    <node concept="M6xJ_" id="4PXMYyZ4PZh" role="lGtFl">
      <property role="Hh88m" value="markerIface" />
      <node concept="tn0Fv" id="4PXMYyZ4PZj" role="HhnKV" />
      <node concept="trNpa" id="4PXMYyZ4PZl" role="EQaZv">
        <ref role="trN6q" node="h0PlHMJ" resolve="InterfaceConceptDeclaration" />
      </node>
    </node>
    <node concept="t5JxF" id="4PXMYyZ6LAb" role="lGtFl">
      <property role="t5JxN" value="Denotes a 'lightweight' interface declaration that doesn'r require 'extends' relation for a language implementing the one." />
    </node>
  </node>
  <node concept="1TIwiD" id="49lEozayI3W">
    <property role="EcuMT" value="4779912984502395132" />
    <property role="TrG5h" value="StructureAspectDeputy" />
    <property role="3GE5qa" value="descriptor" />
    <property role="19KtqR" value="true" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="PrWs8" id="49lEozazjiG" role="PzmwI">
      <ref role="PrY4T" to="yv2q:5Z6N5M4XbRr" resolve="AspectDescriptor" />
    </node>
    <node concept="t5JxF" id="49lEozazjnc" role="lGtFl">
      <property role="t5JxN" value="represents structure aspect in a @descriptor model" />
    </node>
    <node concept="1AhYRh" id="5roO0AoGqED" role="lGtFl" />
  </node>
  <node concept="1TIwiD" id="6VgTGbN6NX2">
    <property role="EcuMT" value="7985135864304582466" />
    <property role="TrG5h" value="ConceptDeclarationReference" />
    <ref role="1TJDcQ" to="tpck:gw2VY9q" resolve="BaseConcept" />
    <node concept="1TJgyj" id="6VgTGbN6NXB" role="1TKVEi">
      <property role="IQ2ns" value="7985135864304582503" />
      <property role="20kJfa" value="cpnt" />
      <property role="20lbJX" value="fLJekj4/_1" />
      <ref role="20lvS9" node="f_TIwhg" resolve="ConceptDeclaration" />
    </node>
    <node concept="t5JxF" id="6VgTGbN6Plf" role="lGtFl">
      <property role="t5JxN" value="Generic 'smart' reference for concepts" />
    </node>
  </node>
</model>

