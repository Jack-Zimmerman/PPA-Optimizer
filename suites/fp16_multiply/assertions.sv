module fp16_multiply_sva(
  input logic clk, input logic rst, input logic [15:0] a, input logic [15:0] b,
  input logic en, input logic [15:0] y, input logic valid);
  logic [1:0] age = 2'd0;
  always @(posedge clk) if (age != 2'd3) age <= age + 2'd1;

  // reset clears valid after the edge
  assert property (@(posedge clk) rst |=> !valid);
  // valid is en delayed by 3 sampling edges, when no reset intervened
  assert property (@(posedge clk)
    (age == 2'd3 && !$past(rst,1) && !$past(rst,2) && !$past(rst,3)) |-> (valid == $past(en,3)));
endmodule
